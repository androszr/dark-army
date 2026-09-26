"""Read live Codex threads from the rollout journal.

Codex writes one append-only JSONL file per thread below ``~/.codex/sessions``.
This is the passive integration seam: it sees CLI, app, and IDE threads without
Dark Army launching or owning their app server.  A rollout has no durable "the client
closed" record, so recency is intentionally part of liveness; stale journals
disappear instead of becoming immortal rows.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
import unicodedata
from copy import deepcopy
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from psutil._psposix import get_terminal_map

_PSUTIL_PROCESS_TYPE = psutil.Process

from . import usage_hold
from .session_stats import (
    AgentInfo, SessionStats, _tail_prose, _collapse_prose, _TLDR_RE,
    _ACTIONS_RE, _parse_actions, _work_report, MAX_SUMMARY_CHARS, questions_from_tool_input,
    quiet_close_out,
)


SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SESSION_INDEX_PATH = Path.home() / ".codex" / "session_index.jsonl"
SESSION_INDEX_TAIL_BYTES = 256 * 1024
LIVE_GRACE_SECONDS = 15 * 60.0
# Account windows last days. LIVE_GRACE is the roster's "is this session
# still here"; applying it here blanked Codex's chips the moment the last
# Codex terminal went quiet, which is the opposite of a weekly budget.
USAGE_GRACE_SECONDS = 7 * 24 * 3600.0
MAX_FILES = 64
# How long after a native Codex process started its own rollout may begin
# before the two stop being plausibly the same session. Generous: the
# journal is written when the first turn starts, not when the binary did.
NEAREST_START_SECONDS = 300.0
_CACHE: dict[Path, tuple[int, int, "CodexRecord"]] = {}
# One process-table reading is good for this long. Three callers per push
# (titles, navigation proofs, the roster's liveness pass) used to walk ~600
# processes each; they now share one walk. A roster change bypasses it
# (`load_recent`), and Jump (`navigation_targets`) always scans fresh.
PROCESS_SNAPSHOT_TTL_SECONDS = 5.0
# `(monotonic stamp, list)` of the last healthy scan. `None` — enumeration
# unavailable — is never stored, so the next caller retries at once.
_PROCESS_SNAPSHOT: Optional[tuple[float, list]] = None
# The safe-root session ids `load_recent` last attached processes to.
_LAST_ROOT_IDS: frozenset = frozenset()
# `(the shared scan's list, its members, {kind: candidates},
# {raw exe: realpath})`: the shared reading classified once per kind
# (`_shared_classification`). Keyed on the list object itself, which it
# keeps, so its identity cannot be reused. The members alone are no key:
# psutil's `process_iter` hands back the same `Process` object per pid in
# every scan, rewriting only `.info`, so a new scan with an unchanged pid
# set holds the very same objects.
_CLASSIFIED: Optional[tuple[list, tuple, dict, dict]] = None
# Guards `_CACHE`, `_PROCESS_SNAPSHOT` and `_LAST_ROOT_IDS`: `load_recent` runs
# on the loop (inline refresh) and on the executor (`_load_rosters`).
# Reentrant because `load_recent` reaches the shared scan through
# `attach_process_ids`. Held for at most one scan; never across an await.
_CACHE_LOCK = threading.RLock()
_USAGE_CACHE: dict[Path, tuple[int, int, float, list[dict]]] = {}
_USAGE_CACHE_LOCK = threading.Lock()
_NON_TUI_COMMANDS = frozenset({
    "a", "app", "app-server", "apply", "archive", "cloud", "completion",
    "code-mode-host", "debug", "delete", "doctor", "e", "exec",
    "exec-server", "features", "help", "login", "logout", "mcp",
    "mcp-server", "plugin",
    "remote-control", "review", "sandbox", "unarchive", "update",
})
_CODEX_OPTIONS_WITH_VALUE = frozenset({
    "-a", "--add-dir", "--ask-for-approval", "-c", "-C", "--cd",
    "--config", "--disable", "--enable", "-i", "--image",
    "--local-provider", "-m", "--model", "-p", "--profile", "--remote",
    "--remote-auth-token-env", "-s", "--sandbox",
})


def _ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("text")
        if item.get("type") in ("input_text", "output_text") and isinstance(value, str):
            parts.append(value)
    return "\n".join(parts).strip()


def _tool_input(payload: dict) -> dict:
    value = payload.get("arguments") if "arguments" in payload else payload.get("input")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _is_synthetic_prompt(text: str) -> bool:
    """Codex serialises injected repository context as a user message first."""
    lead = text.lstrip()
    plugin_close = "</recommended_plugins>"
    plugin_block = (
        lead.startswith("<recommended_plugins>")
        and plugin_close in lead
        and not lead.split(plugin_close, 1)[1].strip()
    )
    return (
        lead.startswith("# AGENTS.md instructions for ")
        or lead.startswith("<INSTRUCTIONS>")
        or lead.startswith("<environment_context>")
        or plugin_block
    )


def _int(value) -> int:
    """A usage figure, or 0 for anything that is not one.

    `grok_usage._int`'s job: a single non-numeric value in one `token_count`
    line must not raise out of `parse_rollout` and stick the whole roster on
    that journal."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _source_kind(value) -> str:
    """Normalise Codex's string/dict session source into one internal label."""
    if isinstance(value, str):
        return value.strip().lower()
    if not isinstance(value, dict):
        return ""
    if any(str(key).lower() == "subagent" for key in value):
        return "subagent"
    for key in ("kind", "type"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item.strip().lower()
    for key in value:
        if str(key).lower() in ("cli", "app", "ide"):
            return str(key).lower()
    return ""


@dataclass(frozen=True)
class CodexProcessIdentity:
    """The process facts Dark Army must be able to replay before acting on a CLI."""

    pid: int
    executable: str
    cwd: str
    create_time: float
    argv: tuple[str, ...]
    match_kind: str


@dataclass
class CodexRecord:
    session_id: str
    thread_id: str
    path: Path
    cwd: str = ""
    title: str = ""
    model: str = ""
    started_at: Optional[float] = None
    last_event: float = 0.0
    activity: str = "waiting"
    current_tool: str = ""
    # Private completion scope; never part of snapshots or process controls.
    turn_id: str = ""
    turn_active: bool = False
    pid: Optional[int] = None
    process_identity: Optional[CodexProcessIdentity] = None
    # Exact root presence: True / proven absent False / unavailable None.
    # Independent of pid / process_identity; never a published capability.
    process_seen: Optional[bool] = None
    originator: str = ""
    source_kind: str = ""
    thread_source: str = ""
    revision: tuple[int, int] = (0, 0)
    parent_thread_id: str = ""
    explicit_subagent: bool = False
    review_child: bool = False
    review_text: str = ""
    review_truncated: bool = False
    review_reports: list[dict] = field(default_factory=list)
    review_reports_omitted: int = 0
    agent_nickname: str = ""
    agent_role: str = ""
    agent_depth: int = 0
    agent_path: str = ""
    spawned_paths: dict[str, AgentInfo] = field(default_factory=dict)
    path_events: dict[str, float] = field(default_factory=dict)
    path_requests: dict[str, float] = field(default_factory=dict)
    # Observations survive helper completion; never populated from workflow/text.
    observed_roles: list[str] = field(default_factory=list)
    children: dict[str, AgentInfo] = field(default_factory=dict)
    child_events: dict[str, float] = field(default_factory=dict)
    child_requests: dict[str, float] = field(default_factory=dict)
    turn_observed: bool = False
    turn_started_at: float = 0.0
    turn_completed_at: float = 0.0
    turn_completed_id: str = ""
    journal_complete: bool = True
    # The last assistant message ended on close-out's "left open" line with
    # no summary or choices (`session_stats.quiet_close_out`): a finished plan
    # or scout run waiting on nobody. Reset by the next real user prompt.
    # Private to the category read; never published on a row or snapshot.
    left_open: bool = False
    # The pending question came from `request_user_input_async`: Codex keeps
    # it queued after the turn ends and takes the person's next ordinary
    # message as the answer (a real user reply releases it below). Private;
    # it widens only the reply route (`stopped_turn(awaiting_answer=True)`),
    # never close authority, and is never published on a row or snapshot.
    question_async: bool = False
    metrics: dict = field(default_factory=dict)
    limit_bars: list[dict] = field(default_factory=list)
    stats: SessionStats = field(default_factory=SessionStats)

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else ""

    @property
    def is_child(self) -> bool:
        return (self.explicit_subagent or bool(self.parent_thread_id)
                or self.source_kind == "subagent" or self.thread_source.strip() == "subagent")

    @property
    def kind(self) -> str:
        return "background" if self.is_child else "interactive"


# Only these measured/compatibility wire names are interpreted as questions.
_QUESTION_TOOLS = {
    "request_user_input": False, "functions.request_user_input": False,
    "request_user_input_async": True, "functions.request_user_input_async": True,
}
_FINISHED_CHILD = frozenset({"completed", "interrupted", "errored", "shutdown", "notFound", "failed"})
_MAX_OBSERVED_ROLES = 64


def _observe_role(record: CodexRecord, role: str) -> None:
    if (role and role != "Codex agent" and len(role) <= 100
            and role not in record.observed_roles
            and len(record.observed_roles) < _MAX_OBSERVED_ROLES):
        record.observed_roles.append(role)


def _question_input(payload: dict, asynchronous: bool) -> dict:
    item = _tool_input(payload)
    raw = item.get("questions")
    if not isinstance(raw, list):
        return {}
    questions = []
    for q in raw[:8]:
        if not isinstance(q, dict) or not isinstance(q.get("options", []), list):
            return {}
        text = q.get("title" if asynchronous else "question")
        if not isinstance(text, str) or not text.strip():
            return {}
        if asynchronous:
            options = q.get("options", [])
            if not all(isinstance(o, str) for o in options):
                return {}
            q = {**q, "question": q.get("title"),
                 "options": [{"label": o} for o in options[:8]]}
        questions.append(q)
    return {"questions": questions}


def parse_rollout(path: Path) -> Optional[CodexRecord]:
    """Parse one journal. Malformed and half-written tail lines are ignored."""
    record: Optional[CodexRecord] = None
    stats = SessionStats()
    tools: Counter[str] = Counter()
    open_tools: dict[str, str] = {}
    first_prompt = ""
    turn_open = False
    turn_id = ""
    pending_spawns: dict[str, tuple[dict, float]] = {}
    pending_followups: dict[str, tuple[str, float]] = {}
    question_async = False
    journal_complete = True
    left_open = False

    try:
        lines = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with lines:
        for line in lines:
            if not line.endswith("\n"):
                journal_complete = False
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                journal_complete = False
                continue
            if not isinstance(obj, dict):
                journal_complete = False
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                journal_complete = False
                continue
            stamp = _ts(obj.get("timestamp"))
            if stamp is not None:
                stats.first_ts = stats.first_ts or stamp
                stats.last_ts = stamp

            outer = obj.get("type")
            event = payload.get("type")
            if outer == "session_meta":
                thread_id = str(payload.get("id") or payload.get("session_id") or "")
                if not thread_id or record is not None:
                    continue
                record = CodexRecord(
                    session_id=f"codex:{thread_id}", thread_id=thread_id,
                    path=path, cwd=str(payload.get("cwd") or ""), stats=stats,
                    originator=str(payload.get("originator") or "").lower(),
                    source_kind=_source_kind(payload.get("source")),
                    thread_source=str(
                        payload.get("thread_source")
                        or payload.get("threadSource")
                        or ""
                    ).lower(),
                )
                record.started_at = stamp.timestamp() if stamp else None
                source = payload.get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                if subagent is None and isinstance(source, dict):
                    subagent = source.get("subAgent")
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                record.explicit_subagent = (isinstance(source, dict)
                    and ("subagent" in source or "subAgent" in source)) or "parent_thread_id" in payload
                record.review_child = subagent == "review"
                parents = [value for present, value in (
                    ("parent_thread_id" in payload, payload.get("parent_thread_id")),
                    (isinstance(spawn, dict) and "parent_thread_id" in spawn,
                     spawn.get("parent_thread_id") if isinstance(spawn, dict) else None),
                ) if present]
                if (parents and all(isinstance(p, str) and p.strip() == p and p
                                    and len(p) <= 200 and not any(c.isspace() for c in p)
                                    and p != thread_id for p in parents)
                        and len(set(parents)) == 1):
                    record.parent_thread_id = parents[0]
                if isinstance(spawn, dict):
                    record.agent_nickname = str(spawn.get("agent_nickname") or "")
                    record.agent_role = str(spawn.get("agent_role") or "")
                    record.agent_path = str(spawn.get("agent_path") or "")
                    try:
                        record.agent_depth = max(0, int(spawn.get("depth") or 0))
                    except (TypeError, ValueError):
                        record.agent_depth = 0
            elif outer == "turn_context":
                stats.cwd = str(payload.get("cwd") or stats.cwd)
                stats.effort = str(payload.get("reasoning_effort") or stats.effort)
                model = payload.get("model")
                if isinstance(model, str) and model:
                    stats.model = model
                    stats.models = [model]
            elif outer == "event_msg" and event == "task_started":
                turn_open = True
                if record is not None:
                    record.turn_observed = True
                    record.turn_started_at = stamp.timestamp() if stamp else 0.0
                    record.turn_completed_at = 0.0
                    record.turn_completed_id = ""
                turn_id = str(payload.get("turn_id") or "")
                open_tools.clear()
                pending_spawns.clear()
                pending_followups.clear()
                stats.question = {}
                stats.questions = []
                stats.user_prompts += 1
            elif outer == "event_msg" and event in (
                "task_complete", "task_completed", "turn_aborted"
            ):
                ended_id = str(payload.get("turn_id") or "")
                if ended_id and turn_id and ended_id != turn_id:
                    continue
                turn_open = False
                if record is not None:
                    record.turn_observed = True
                    record.turn_completed_at = stamp.timestamp() if stamp else 0.0
                    record.turn_completed_id = ended_id
                open_tools.clear()
                pending_spawns.clear()
                pending_followups.clear()
                # Async questions remain queued in Codex after a normal turn
                # completion. Ending the assistant's work is not a human answer.
                if not question_async or event == "turn_aborted":
                    stats.question = {}
                    stats.questions = []
            elif outer == "event_msg" and event == "user_message":
                text = str(payload.get("message") or "")
                if record is not None and text and not _is_synthetic_prompt(text):
                    record.turn_completed_id = ""
                    if not turn_open:
                        record.turn_started_at = 0.0
            elif outer == "event_msg" and event == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage") or {}
                if isinstance(usage, dict):
                    stats.input_tokens = _int(usage.get("input_tokens"))
                    stats.output_tokens = _int(usage.get("output_tokens"))
                    stats.cache_read_tokens = _int(usage.get("cached_input_tokens"))
                    stats.cache_creation_tokens = _int(usage.get("cache_write_input_tokens"))
                if record is not None and isinstance(info, dict):
                    last = info.get("last_token_usage") or {}
                    window = info.get("model_context_window")
                    used = last.get("total_tokens") if isinstance(last, dict) else None
                    if isinstance(used, (int, float)) and isinstance(window, (int, float)) and window > 0:
                        record.metrics["ctx_used_pct"] = min(100.0, 100.0 * used / window)
                        record.metrics["context_window"] = int(window)
                    limits = payload.get("rate_limits") or {}
                    candidates = []
                    if isinstance(limits, dict):
                        bars = []
                        for slot, value in (("primary", limits.get("primary")),
                                            ("secondary", limits.get("secondary"))):
                            if not isinstance(value, dict):
                                continue
                            pct = value.get("used_percent")
                            minutes = value.get("window_minutes")
                            if isinstance(pct, (int, float)):
                                candidates.append((float(pct), minutes, value.get("resets_at")))
                                if isinstance(minutes, (int, float)) and minutes > 0:
                                    hours = float(minutes) / 60.0
                                    label = (f"{int(hours / 24)}d" if hours >= 24
                                             else f"{int(hours)}h")
                                else:
                                    label = slot
                                bars.append({
                                    "kind": f"codex_{slot}",
                                    "group": "weekly" if label.endswith("d") else "session",
                                    "provider": "codex",
                                    "title": f"Codex, {label}",
                                    "label": label,
                                    "percent": float(pct),
                                    "resets_at": float(value["resets_at"])
                                    if isinstance(value.get("resets_at"), (int, float)) else None,
                                    "stale": False,
                                })
                        if record is not None and bars:
                            record.limit_bars = bars
                    if candidates:
                        pct, minutes, resets = max(candidates, key=lambda item: item[0])
                        record.metrics["five_hour_pct"] = pct
                        if isinstance(resets, (int, float)):
                            record.metrics["five_hour_resets_at"] = float(resets)
                        if isinstance(minutes, (int, float)):
                            days = minutes / 1440
                            record.metrics["budget_cycle"] = (
                                f"{int(days)}d" if days >= 1 else f"{int(minutes / 60)}h"
                            )
            elif outer == "event_msg" and event in ("item_started", "item_completed"):
                item = payload.get("item") or {}
                if not isinstance(item, dict) or item.get("type") != "collabAgentToolCall":
                    continue
                prompt = str(item.get("prompt") or "")
                model = str(item.get("model") or "")
                states = item.get("agentsStates") or {}
                receivers = item.get("receiverThreadIds") or []
                if not isinstance(receivers, list):
                    receivers = []
                for child in receivers:
                    child = str(child or "")
                    if not child:
                        continue
                    state = states.get(child) if isinstance(states, dict) else {}
                    status = str(state.get("status") or "") if isinstance(state, dict) else ""
                    if record is not None:
                        record.child_events[child] = stamp.timestamp() if stamp else 0.0
                    if status in _FINISHED_CHILD:
                        stats.agents.pop(child, None)
                    else:
                        known = record.children.get(child) if record else None
                        stats.agents[child] = AgentInfo(
                            agent_id=child,
                            description=prompt or (known.description if known else ""),
                            subagent_type=known.subagent_type if known else "Codex agent",
                            model=model,
                            activity=status or "running",
                        )
            elif outer == "response_item" and event == "message":
                role = payload.get("role")
                text = _text(payload.get("content"))
                if role == "user" and text and not _is_synthetic_prompt(text):
                    first_prompt = first_prompt or text
                    if record is not None:
                        # Input may precede task_started in a partially flushed
                        # journal. The previous turn can no longer grant close.
                        record.turn_completed_id = ""
                        if not turn_open:
                            record.turn_started_at = 0.0
                    stats.last_report = ""
                    left_open = False
                    stats.last_summary = ""
                    stats.last_actions = []
                    # Native async answers arrive as later user input. Context
                    # injection is excluded above; an acknowledgement is not here.
                    stats.question = {}
                    stats.questions = []
                elif role == "assistant" and text:
                    if record is not None and record.review_child:
                        raw = text.encode("utf-8")
                        record.review_text = raw[:16384].decode("utf-8", errors="ignore")
                        record.review_truncated = len(raw) > 16384
                    report = _work_report(text)
                    if report:
                        stats.last_report = report
                    left_open = quiet_close_out(text)
                    marks = _TLDR_RE.findall(text)
                    offers = _ACTIONS_RE.findall(text)
                    prose = _collapse_prose(_ACTIONS_RE.sub(" ", _TLDR_RE.sub(" ", text)))
                    if prose:
                        stats.last_text = _tail_prose(prose)
                    if marks or prose:
                        stats.last_summary = (
                            " ".join(marks[-1].split())[:MAX_SUMMARY_CHARS] if marks else "")
                    if offers or prose:
                        stats.last_actions = _parse_actions(offers[-1]) if offers else []
                    stats.assistant_messages += 1
            elif outer == "response_item" and event in ("custom_tool_call", "function_call"):
                if record is not None:
                    record.turn_completed_id = ""
                    if not turn_open:
                        record.turn_started_at = 0.0
                name = str(payload.get("name") or "")
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                if name:
                    tools[name] += 1
                    open_tools[call_id] = name
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                event_turn = metadata.get("turn_id") if isinstance(metadata, dict) else ""
                current_turn = not event_turn or not turn_id or event_turn == turn_id
                if name in _QUESTION_TOOLS and call_id and current_turn:
                    asynchronous = _QUESTION_TOOLS[name]
                    questions = questions_from_tool_input(_question_input(payload, asynchronous), call_id)
                    if questions:
                        question_async = asynchronous
                        stats.questions = questions
                        stats.question = {k: v for k, v in questions[0].items() if k != "index"}
                if name in ("spawn_agent", "collaboration.spawn_agent") and call_id and current_turn:
                    pending_spawns[call_id] = (_tool_input(payload), stamp.timestamp() if stamp else 0.0)
                if name in ("followup_task", "collaboration.followup_task", "send_input") and call_id and current_turn:
                    args = _tool_input(payload)
                    target = args.get("target") or args.get("id")
                    if isinstance(target, str):
                        pending_followups[call_id] = (target, stamp.timestamp() if stamp else 0.0)
            elif outer == "response_item" and event in (
                "custom_tool_call_output", "function_call_output"
            ):
                call_id = str(payload.get("call_id") or "")
                open_tools.pop(call_id, None)
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                event_turn = metadata.get("turn_id") if isinstance(metadata, dict) else ""
                current_turn = not event_turn or not turn_id or event_turn == turn_id
                if call_id and call_id == stats.question.get("id") and current_turn:
                    result = _tool_input({"input": payload.get("output")})
                    answers = result.get("answers")
                    if isinstance(answers, dict) and not result.get("error"):
                        lines = []
                        shortened = len(answers) > 8
                        for key, value in list(answers.items())[:8]:
                            choices = value.get("answers") if isinstance(value, dict) else None
                            if isinstance(choices, list):
                                shortened |= len(choices) > 8
                                for choice in choices[:8]:
                                    if isinstance(choice, str):
                                        shortened |= len(str(key)) > 200 or len(choice) > 2000
                                        lines.append(str(key)[:200] + ": " + choice[:2000])
                        outcome = "\n".join(lines)
                        if outcome:
                            stats.question_results.append({"source_id": call_id,
                                "outcome": outcome[:2000], "truncated": shortened or len(outcome) > 2000})
                            del stats.question_results[:-32]
                    # Sync output completes the call. Async accepted:true only
                    # registers it; unknown outputs cannot release a close guard.
                    if (not question_async or isinstance(answers, dict)
                            or result.get("cancelled") is True or result.get("canceled") is True
                            or result.get("error") or result.get("accepted") is False):
                        stats.question = {}
                        stats.questions = []
                followup = pending_followups.pop(call_id, None)
                if followup and record is not None:
                    target, requested_at = followup
                    output = _tool_input({"input": payload.get("output")})
                    success = bool(output) and not output.get("error") and output.get("accepted") is not False
                    # Native relative targets return the authoritative absolute path.
                    canonical = output.get("task_name")
                    task_path = canonical if isinstance(canonical, str) and canonical.startswith("/") else target
                    if success and (task_path.startswith("/") or target not in record.children):
                        # An unresolved relative target refuses close without guessing
                        # its child. A later canonical result can settle that alias.
                        if task_path != target and not target.startswith("/"):
                            record.path_events.pop(target, None)
                            record.path_requests.pop(target, None)
                        record.path_events[task_path] = stamp.timestamp() if stamp else 0.0
                        record.path_requests[task_path] = requested_at
                    if success and target in record.children:
                        stats.agents[target] = deepcopy(record.children[target])
                        stats.agents[target].activity = "running"
                        record.child_events[target] = stamp.timestamp() if stamp else 0.0
                        record.child_requests[target] = requested_at
                spawn_call = pending_spawns.pop(call_id, None)
                if spawn_call is not None:
                    spawn, requested_at = spawn_call
                    output = _tool_input({"input": payload.get("output")})
                    success = bool(output) and not output.get("error") and output.get("accepted") is not False
                    child = output.get("agent_id") or output.get("thread_id")
                    task_path = output.get("task_name")
                    if (record is not None and isinstance(task_path, str) and task_path.startswith("/")
                            and success):
                        info = AgentInfo(agent_id="", description=str(spawn.get("message") or ""),
                                         subagent_type=str(spawn.get("agent_type") or "Codex agent"),
                                         model=str(spawn.get("model") or ""))
                        record.spawned_paths[task_path] = info
                        record.path_events[task_path] = stamp.timestamp() if stamp else 0.0
                        record.path_requests[task_path] = requested_at
                        _observe_role(record, info.subagent_type)
                    if isinstance(child, str) and child and success:
                        stats.agents[child] = AgentInfo(
                            agent_id=child,
                            description=str(spawn.get("message") or ""),
                            subagent_type=str(spawn.get("agent_type") or "Codex agent"),
                            model=str(spawn.get("model") or ""),
                        )
                        if record is not None:
                            record.children[child] = deepcopy(stats.agents[child])
                            record.child_events[child] = stamp.timestamp() if stamp else 0.0
                            _observe_role(record, stats.agents[child].subagent_type)

    if record is None:
        return None
    record.journal_complete = journal_complete
    record.left_open = left_open
    record.question_async = bool(question_async and (stats.question or stats.questions))
    stats.tool_counts = dict(tools)
    record.cwd = stats.cwd or record.cwd
    record.model = stats.model or ""
    record.title = " ".join(first_prompt.split())[:160]
    record.activity = "working" if turn_open and not stats.question else "waiting"
    record.current_tool = next(reversed(open_tools.values()), "")
    record.turn_id = turn_id
    record.turn_active = turn_open
    try:
        record.last_event = path.stat().st_mtime
    except OSError:
        record.last_event = stats.last_ts.timestamp() if stats.last_ts else 0.0
    return record


def _clean_thread_name(value) -> str:
    if not isinstance(value, str):
        return ""
    value = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in value
    )
    return " ".join(value.split())[:160]


def _thread_names(path: Optional[Path] = None) -> dict[str, str]:
    """Newest non-empty Codex name per thread from a bounded JSONL tail."""
    path = SESSION_INDEX_PATH if path is None else path
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - SESSION_INDEX_TAIL_BYTES)
            if start:
                stream.seek(start - 1)
                previous = stream.read(1)
            else:
                previous = b"\n"
            stream.seek(start)
            data = stream.read(SESSION_INDEX_TAIL_BYTES)
    except OSError:
        return {}
    if start and previous != b"\n":
        split = data.find(b"\n")
        if split < 0:
            return {}
        data = data[split + 1:]

    names: dict[str, str] = {}
    for line in data.splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        thread_id = item.get("id")
        name = _clean_thread_name(item.get("thread_name"))
        if isinstance(thread_id, str) and thread_id and name:
            names[thread_id] = name
    return names


def load_recent(
    root: Optional[Path] = None,
    *, now: Optional[float] = None,
    grace: float = LIVE_GRACE_SECONDS,
) -> list[CodexRecord]:
    """Newest active-looking rollouts, bounded before their contents are read."""
    root = SESSIONS_DIR if root is None else root
    clock = time.time() if now is None else now
    try:
        paths = sorted(
            root.glob("*/*/*/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:MAX_FILES]
    except OSError:
        return []
    # Computed before the cache lock: `process_snapshot_shared` takes it too.
    held = _held_quiet_journals(paths, clock, grace)
    thread_names = _thread_names()
    out = []
    seen_threads: set[str] = set()
    duplicate_threads: set[str] = set()
    live_paths = set(paths)
    with _CACHE_LOCK:
        for cached in list(_CACHE):
            if cached not in live_paths:
                _CACHE.pop(cached, None)
        for path in paths:
            try:
                stat = path.stat()
                if clock - stat.st_mtime > grace and (
                        os.path.realpath(path), stat.st_dev, stat.st_ino) not in held:
                    continue
            except OSError:
                continue
            key = (stat.st_mtime_ns, stat.st_size)
            cached = _CACHE.get(path)
            if cached is not None and cached[:2] == key:
                record = deepcopy(cached[2])
            else:
                record = parse_rollout(path)
                if record is not None:
                    record.revision = key
                    _CACHE[path] = (*key, record)
                    record = deepcopy(record)
            if record is not None and record.thread_id in seen_threads:
                duplicate_threads.add(record.thread_id)
            if record is not None and record.thread_id not in seen_threads:
                record.revision = key
                record.title = thread_names.get(record.thread_id, record.title)
                out.append(record)
                # ``paths`` is newest-first. A thread can have replacement journals;
                # retain the first one so an older path cannot overwrite its roster
                # record or make process attachment look ambiguous.
                seen_threads.add(record.thread_id)
    # A child has its own journal, but it belongs under its root row. Its source
    # is more authoritative than a parent's possibly older collaboration item,
    # and fills the label/model even when Dark Army first sees the child mid-turn.
    by_thread = {record.thread_id: record for record in out}
    # Validate the entire ancestry before any attachment. A cycle or project
    # mismatch must not leak a child into a root's helpers or review evidence.
    def ancestry(child):
        chain, seen = [], {child.thread_id}
        if child.thread_id in duplicate_threads:
            return ()
        current = child
        while current.parent_thread_id:
            parent = by_thread.get(current.parent_thread_id)
            if (parent is None or parent.thread_id in seen or parent.thread_id in duplicate_threads or not current.cwd
                    or Path(current.cwd).resolve() != Path(parent.cwd).resolve()):
                return ()
            chain.append(parent)
            seen.add(parent.thread_id)
            current = parent
        return tuple(chain) if not current.is_child else ()

    for child in out:
        chain = ancestry(child)
        if not chain:
            continue
        parent = chain[0]
        known = parent.children.get(child.thread_id) or parent.spawned_paths.get(child.agent_path)
        role = (known.subagent_type if known and known.subagent_type != "Codex agent"
                else child.agent_role or "Codex agent")
        if not child.review_child:
            _observe_role(parent, role)
        info = AgentInfo(
            agent_id=child.thread_id, description=child.title or (known.description if known else ""),
            subagent_type=role, model=child.model or (known.model if known else ""),
            parent_agent_id="", activity=child.activity,
        )
        parent.children[child.thread_id] = info
        # A completed retained journal is history, not a working helper. A
        # newer explicit parent follow-up can resume it before its next write.
        child_stamp = child.stats.last_ts.timestamp() if child.stats.last_ts else 0.0
        followed_at = parent.path_events.get(child.agent_path, 0.0)
        parent_stamp = max(parent.child_events.get(child.thread_id, 0.0), followed_at)
        requests = [requested for key, requested in parent.child_requests.items() if key == child.thread_id]
        requests += [requested for key, requested in parent.path_requests.items() if key == child.agent_path]
        if requests:
            if all(requests) and _child_request_settled(parent, child, max(requests)):
                parent.stats.agents.pop(child.thread_id, None)
            else:
                info.activity = child.activity if child.turn_active else "running"
                parent.stats.agents[child.thread_id] = info
        elif parent_stamp > child_stamp:
            if child.thread_id in parent.stats.agents or followed_at > child_stamp:
                info.activity = "running"
                parent.stats.agents[child.thread_id] = info
        elif child.turn_active or not child.turn_observed:
            parent.stats.agents[child.thread_id] = info
        else:
            parent.stats.agents.pop(child.thread_id, None)
    # Fold actual descendant rows through completed ancestors after every direct
    # merge. Snapshot inputs make this independent of journal discovery order.
    direct = {r.thread_id: (deepcopy(r.stats.agents), tuple(r.observed_roles)) for r in out}
    for descendant in sorted(out, key=lambda r: (r.started_at or 0.0, r.thread_id)):
        ancestors = ancestry(descendant)
        agents, roles = direct[descendant.thread_id]
        for ancestor in ancestors:
            for role in roles:
                _observe_role(ancestor, role)
            for aid, info in agents.items():
                if aid != ancestor.thread_id and info.activity not in _FINISHED_CHILD:
                    adopted = deepcopy(info)
                    adopted.parent_agent_id = adopted.parent_agent_id or descendant.thread_id
                    ancestor.stats.agents.setdefault(aid, adopted)
    # Review content is retained separately from live helpers and root output.
    # Four * 16 KiB is also the combined bound; decoding a cut JSON is forbidden.
    for child in sorted(out, key=lambda r: (r.last_event, r.thread_id), reverse=True):
        chain = ancestry(child)
        if not chain or not child.review_child or not stopped_turn(child) or not child.review_text:
            continue
        parent = chain[-1]
        if len(parent.review_reports) >= 4:
            parent.review_reports_omitted += 1
            continue
        parent.review_reports.append({
            "session_id": child.session_id, "label": "Codex review",
            "text": child.review_text,
            "truncated": child.review_truncated,
        })
    # A root appearing or vanishing gets a fresh scan: a new Codex session
    # must never be tombstoned "no process" off a reading taken before its
    # process existed.
    global _LAST_ROOT_IDS
    ids = frozenset(r.session_id for r in out if _safe_cli_root(r))
    with _CACHE_LOCK:
        if ids != _LAST_ROOT_IDS:
            invalidate_process_snapshot()
            _LAST_ROOT_IDS = ids
    if ids:
        attach_process_ids(out)
    return out


#: (the shared scan's list, the journals its native Codex processes hold).
_HELD_MEMO: Optional[tuple[list, frozenset]] = None


def _held_quiet_journals(paths, clock: float, grace: float) -> set:
    """Journal identities past ``grace`` that a live native Codex TUI holds open.

    The grace is how a *closed* session leaves the roster. A finished turn
    whose tab is still open goes just as quiet, and dropping it lost the one
    record every Close path needs (`_codex_human_close_candidate`): fifteen
    minutes after a Codex run finished, its terminal could no longer be
    closed from Dark Army at all, only hidden, and the tab stayed open. A
    journal the running process still holds is still a live session, so it
    stays scanned — and leaves on the first scan after that process exits.

    Empty unless some journal is actually past the grace. Past it — the
    usual case, since the newest `MAX_FILES` journals are mostly old — the
    open-file walk costs one pass per shared process scan, not one per
    roster refresh: the answer is memoised on that scan's list object
    (`_HELD_MEMO`). A process table that cannot be read keeps nothing (the
    old behaviour), never everything.
    """
    stale = []
    for path in paths:
        try:
            if clock - path.stat().st_mtime > grace:
                stale.append(path)
        except OSError:
            continue
    if not stale:
        return set()
    global _HELD_MEMO
    snapshot = process_snapshot_shared()
    if not snapshot:
        return set()
    memo = _HELD_MEMO
    if memo is not None and memo[0] is snapshot:
        return set(memo[1])
    held = set()
    for proc, _identity in _native_processes(snapshot):
        for identity in _open_identities(proc) or ():
            held.add(identity)
    # The memo keeps the list itself, so its identity cannot be reused.
    _HELD_MEMO = (snapshot, frozenset(held))
    return held


def _safe_cli_root(record: CodexRecord) -> bool:
    return (
        not record.is_child
        and record.originator == "codex-tui"
        and record.source_kind == "cli"
        and record.thread_source == "user"
    )


def _explicit_resume(argv: tuple[str, ...], thread_id: str) -> bool:
    return any(
        arg == "resume" and index + 1 < len(argv) and argv[index + 1] == thread_id
        for index, arg in enumerate(argv)
    )


def _is_tui_invocation(argv: tuple[str, ...], *, start: int = 1) -> bool:
    """Classify the first positional command, never later prompt tokens."""
    index = start
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return True
        if arg in _CODEX_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg not in _NON_TUI_COMMANDS
    return True


def _process_field(proc, name: str):
    info = getattr(proc, "info", {}) or {}
    value = info.get(name)
    if value is not None:
        return value
    method = getattr(proc, name, None)
    return method() if callable(method) else None


def _process_snapshot() -> Optional[list[object]]:
    """One scan: [] proves absence; None means enumeration was unavailable."""
    try:
        # psutil memoizes /dev/tty* for the process lifetime. A terminal born
        # after the daemon started otherwise reads as None, making one new
        # native Codex an uncertain competitor for every root in its project.
        # Refresh once before process_iter materializes its terminal fields;
        # exact journal/PID/tty revalidation still applies to every candidate.
        get_terminal_map.cache_clear()
        return list(psutil.process_iter(
            ["pid", "name", "exe", "cwd", "create_time", "cmdline", "terminal"]
        ))
    except (OSError, PermissionError, psutil.Error):
        return None


def process_snapshot_shared(ttl: float = PROCESS_SNAPSHOT_TTL_SECONDS) -> Optional[list[object]]:
    """The last healthy scan while younger than ``ttl``, else a fresh one.

    `[]` (a healthy empty table) is cached like any other reading; `None`
    (enumeration unavailable) is returned and never cached, so the next caller
    retries. Only the candidate list is shared: identity, journal ownership and
    the double observation are still revalidated per call, off fresh process
    objects (`_fresh_liveness_identity`).
    """
    global _PROCESS_SNAPSHOT
    with _CACHE_LOCK:
        memo = _PROCESS_SNAPSHOT
        now = time.monotonic()
        if memo is not None and now - memo[0] < ttl:
            return memo[1]
        observed = _process_snapshot()
        if isinstance(observed, list):
            _PROCESS_SNAPSHOT = (now, observed)
        return observed


def invalidate_process_snapshot() -> None:
    """Forget the shared scan; the next shared read walks the table again."""
    global _PROCESS_SNAPSHOT
    with _CACHE_LOCK:
        _PROCESS_SNAPSHOT = None


def _normalise_tty(value) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "??" or "?" in raw:
        return ""
    normalised = os.path.normpath(
        raw if raw.startswith("/dev/") else f"/dev/{raw}"
    )
    return normalised if normalised.startswith("/dev/") else ""


def _node_codex_launcher(executable: str, argv: tuple[str, ...]) -> bool:
    """Recognise the installed Node CLI launcher, never a generic Node job."""
    if Path(executable).name.lower() not in ("node", "nodejs"):
        return False
    for arg in argv[1:]:
        lower = arg.lower().replace("\\", "/")
        path = Path(arg)
        if path.name.lower() == "codex" and path.parent.name == "bin":
            return True
        if path.name.lower() == "codex.js" and "/@openai/codex/" in lower:
            return True
    return False


@dataclass(frozen=True)
class CodexTitleRoot:
    """Immutable decoration input copied from the loop-owned Codex roster."""

    session_id: str
    thread_id: str
    path: Path
    cwd: str


def project_title_roots(records) -> tuple[CodexTitleRoot, ...]:
    """Copy safe root facts without carrying mutable roster/control state."""
    return tuple(
        CodexTitleRoot(
            session_id=record.session_id,
            thread_id=record.thread_id,
            path=Path(record.path),
            cwd=record.cwd,
        )
        for record in records
        if _safe_cli_root(record)
    )


@dataclass(frozen=True)
class _CodexTitleProcess:
    process: object
    pid: int
    executable: str
    cwd: str
    create_time: float
    argv: tuple[str, ...]
    tty: str
    native: bool


def _realpath(raw: str, memo: dict) -> str:
    """`os.path.realpath`, once per distinct executable per classification:
    ~600 processes name a few dozen executables, and each resolve is an
    `lstat` per path component."""
    resolved = memo.get(raw)
    if resolved is None:
        resolved = memo[raw] = os.path.realpath(raw)
    return resolved


def _same_members(kept: tuple, process_list: list) -> bool:
    return len(kept) == len(process_list) and all(
        a is b for a, b in zip(kept, process_list))


def _shared_classification(process_list, kind: str, classify) -> list:
    """`classify(process_list, realpaths)`, run once per kind per shared scan.

    Titles, navigation proofs, the roster's liveness pass and the held-journal
    walk each filtered the whole process table on every call — a realpath, a
    live `terminal()` read and a launcher test for each of ~600 processes,
    several times a push, which measured as the daemon's largest steady cost.
    The shared reading (`process_snapshot_shared`) is one scan per
    `PROCESS_SNAPSHOT_TTL_SECONDS`; its classification is now one pass per
    kind for the same reading. Only the shared list itself is memoised (by
    identity): a caller's own list — Jump's fresh scan, Stop's proof, a
    test's stand-ins — is classified afresh every time, exactly as before.
    Every caller gets its own list; the candidates themselves are frozen."""
    global _CLASSIFIED
    shared = _PROCESS_SNAPSHOT
    if shared is None or shared[1] is not process_list:
        return classify(process_list, {})
    memo = _CLASSIFIED
    if (memo is None or memo[0] is not process_list
            or not _same_members(memo[1], process_list)):
        # The list is the key: every scan builds a new one, even when psutil
        # hands back the same `Process` objects. The members are checked as
        # well (a tuple of the very objects), so an element replaced in the
        # list — which a scan never does, a test does — is classified again.
        memo = (process_list, tuple(process_list), {}, {})
        _CLASSIFIED = memo
    found = memo[2].get(kind)
    if found is None:
        # Unlocked: two threads racing here each classify once, and either
        # answer is the same reading's.
        found = memo[2][kind] = classify(process_list, memo[3])
    return list(found)


def _title_processes(processes=None) -> list[_CodexTitleProcess]:
    """Safe Codex CLI terminals, separate from process-control authority."""
    process_list = process_snapshot_shared() if processes is None else processes
    if process_list is None:
        return []
    return _shared_classification(process_list, "title", _classify_title)


def _classify_title(process_list, realpaths: dict) -> list[_CodexTitleProcess]:
    candidates = []
    for proc in process_list:
        try:
            pid = int(_process_field(proc, "pid") or getattr(proc, "pid", 0) or 0)
            executable = _realpath(str(_process_field(proc, "exe") or ""), realpaths)
            cwd = str(_process_field(proc, "cwd") or "")
            created = float(_process_field(proc, "create_time"))
            argv = tuple(str(arg) for arg in (_process_field(proc, "cmdline") or ()))
            tty = _normalise_tty(_process_field(proc, "terminal"))
        except (OSError, PermissionError, TypeError, ValueError, psutil.Error):
            continue
        lower_exe = executable.lower()
        native = Path(executable).name == "codex"
        node_launcher = _node_codex_launcher(executable, argv)
        if (
            pid <= 1
            or not executable
            or not cwd
            or not argv
            or not tty
            or ".app/contents/" in lower_exe
            or not _is_tui_invocation(argv, start=2 if node_launcher else 1)
            or not (native or node_launcher)
        ):
            continue
        candidates.append(_CodexTitleProcess(
            process=proc,
            pid=pid,
            executable=executable,
            cwd=cwd,
            create_time=created,
            argv=argv,
            tty=tty,
            native=native,
        ))
    return candidates


def _journal_identity(path: Path | str) -> Optional[tuple[str, int, int]]:
    """Canonical path plus file identity, or None for anything unsafe."""
    try:
        canonical = os.path.realpath(os.fspath(path))
        found = os.stat(canonical)
    except (OSError, PermissionError, TypeError, ValueError):
        return None
    if not stat.S_ISREG(found.st_mode):
        return None
    return canonical, found.st_dev, found.st_ino


def _fresh_title_process(proc) -> Optional[_CodexTitleProcess]:
    """Re-read a process without trusting the process_iter info cache."""
    try:
        if isinstance(proc, _PSUTIL_PROCESS_TYPE):
            proc = psutil.Process(proc.pid)
        pid = int(getattr(proc, "pid", 0) or 0)
        executable = os.path.realpath(str(proc.exe() or ""))
        cwd = str(proc.cwd() or "")
        created = float(proc.create_time())
        argv = tuple(str(arg) for arg in (proc.cmdline() or ()))
        tty = _normalise_tty(proc.terminal())
    except (OSError, PermissionError, TypeError, ValueError, psutil.Error):
        return None
    lower_exe = executable.lower()
    native = Path(executable).name == "codex"
    node_launcher = _node_codex_launcher(executable, argv)
    if (
        pid <= 1
        or not executable
        or not cwd
        or not argv
        or not tty
        or ".app/contents/" in lower_exe
        or not _is_tui_invocation(argv, start=2 if node_launcher else 1)
        or not (native or node_launcher)
    ):
        return None
    return _CodexTitleProcess(
        process=proc,
        pid=pid,
        executable=executable,
        cwd=cwd,
        create_time=created,
        argv=argv,
        tty=tty,
        native=native,
    )


def _open_identities(proc) -> Optional[list[tuple[str, int, int]]]:
    try:
        opened = proc.open_files()
    except (OSError, PermissionError, TypeError, ValueError, psutil.Error):
        return None
    identities = []
    for item in opened or ():
        path = getattr(item, "path", item)
        identity = _journal_identity(path)
        if identity is not None:
            identities.append(identity)
    return identities


def _native_holder_evidence(
    root: CodexTitleRoot,
    candidate: _CodexTitleProcess,
) -> str:
    """Observe one unchanged native process holding one unchanged root file."""
    if not candidate.native or candidate.cwd != root.cwd:
        return "absent"
    target = _journal_identity(root.path)
    if target is None:
        return "unstable"
    opened = _open_identities(candidate.process)
    if opened is None:
        return "unavailable"
    if target not in opened:
        # A hard link proves only inode equality, not that this exact rollout
        # path is the fd Codex opened. Treat it as conflicting evidence so the
        # cwd fallback cannot turn an alias into identity.
        if any(identity[1:] == target[1:] for identity in opened):
            return "unstable"
        return "absent"
    if _journal_identity(root.path) != target:
        return "unstable"
    fresh = _fresh_title_process(candidate.process)
    if fresh is None or (
        fresh.pid,
        fresh.executable,
        fresh.cwd,
        fresh.create_time,
        fresh.argv,
        fresh.tty,
        fresh.native,
    ) != (
        candidate.pid,
        candidate.executable,
        candidate.cwd,
        candidate.create_time,
        candidate.argv,
        candidate.tty,
        candidate.native,
    ):
        return "unstable"
    # Close/reload can race either the process recheck or the second root stat.
    # Re-enumerate once so a journal that stopped being held during the probe is
    # absence, never proof retained from the first observation.
    opened_again = _open_identities(candidate.process)
    if opened_again is None or target not in opened_again:
        return "unstable"
    if _journal_identity(root.path) != target:
        return "unstable"
    return "holder"


@dataclass(frozen=True)
class CodexNavigationProof:
    """Private exact-holder authority for navigation and board attribution only."""

    root: CodexTitleRoot
    journal: tuple[str, int, int]
    pid: int
    executable: str
    create_time: float
    argv: tuple[str, ...]
    tty: str


@dataclass(frozen=True)
class CodexNavigationRoot:
    root: CodexTitleRoot
    control: Optional[CodexProcessIdentity]


def project_navigation_roots(records) -> tuple[CodexNavigationRoot, ...]:
    return tuple(CodexNavigationRoot(
        CodexTitleRoot(r.session_id, r.thread_id, Path(r.path), r.cwd),
        r.process_identity,
    ) for r in records if _safe_cli_root(r))


@dataclass(frozen=True)
class CodexNavigationTarget:
    pid: int
    tty: str
    proof: Optional[CodexNavigationProof] = None


def navigation_targets(roots, expected, allow_new_proofs=False, uncertain=None) -> dict[str, CodexNavigationTarget]:
    """Revalidate captured immutable facts; never mutate the live roster."""
    if not roots:
        return {}
    observed = _process_snapshot()
    if observed is None:
        if uncertain is not None:
            uncertain.update(r.root.session_id for r in roots)
        return {}
    proofs = resolve_navigation_proofs((r.root for r in roots), observed, uncertain=uncertain)
    records = [CodexRecord(
        session_id=r.root.session_id, thread_id=r.root.thread_id,
        path=r.root.path, cwd=r.root.cwd, originator="codex-tui",
        source_kind="cli", thread_source="user", process_identity=r.control,
    ) for r in roots]
    result = {}
    for captured, record in zip(roots, records):
        sid = record.session_id
        proof = proofs.get(sid)
        if sid in expected:
            if proof == expected[sid]:
                result[sid] = CodexNavigationTarget(proof.pid, proof.tty, proof)
            continue  # stale exact evidence must never fall back to cwd
        if allow_new_proofs and proof is not None:
            # Board requests can precede the first snapshot publication. Keep
            # this observation so even a wrong parent cannot fall back to cwd.
            result[sid] = CodexNavigationTarget(proof.pid, proof.tty, proof)
            continue
        if captured.control is not None:
            proc = matching_process_identity(record, captured.control,
                                             destructive=False, processes=observed,
                                             records=records)
            if proc is not None:
                try:
                    tty = _normalise_tty(proc.terminal())
                except (AttributeError, OSError, ValueError, psutil.Error):
                    tty = ""
                result[sid] = CodexNavigationTarget(captured.control.pid, tty)
    return result


def resolve_navigation_proofs(roots, processes=None, *, uncertain=None) -> dict[str, CodexNavigationProof]:
    """One bounded scan, exact native holders only; uncertainty withdraws proof."""
    roots = tuple(roots)
    # Optional worker-local diagnostics preserve absence versus failed/conflicting
    # observation for board fallback. Neither this set nor proofs are published.
    uncertain = set() if uncertain is None else uncertain
    if not roots:
        return {}
    try:
        observed = process_snapshot_shared() if processes is None else list(processes)
    except (OSError, ValueError, psutil.Error):
        uncertain.update(root.session_id for root in roots)
        return {}
    if observed is None:
        uncertain.update(root.session_id for root in roots)
        return {}
    candidates = _title_processes(observed)
    known = {id(item.process) for item in candidates}
    uncertain_cwds = set()
    for proc in observed:
        if id(proc) in known:
            continue
        # A skipped unreadable native process could be a competing holder.
        try:
            name = str(_process_field(proc, "name") or "")
        except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
            name = ""
        if name and name != "codex":
            continue
        try:
            exe = str(_process_field(proc, "exe") or "")
            if exe and (Path(os.path.realpath(exe)).name != "codex"
                        or ".app/contents/" in exe.lower()):
                continue
            argv = tuple(_process_field(proc, "cmdline") or ())
            if argv and not _is_tui_invocation(argv):
                continue
            cwd = str(_process_field(proc, "cwd") or "")
        except (OSError, TypeError, ValueError, psutil.Error):
            cwd = ""
        uncertain_cwds.add(cwd)
    result = {}
    for root in roots:
        if ("" in uncertain_cwds or root.cwd in uncertain_cwds
                or sum(other.session_id == root.session_id for other in roots) != 1):
            uncertain.add(root.session_id)
            continue
        target = _journal_identity(root.path)
        relevant = [item for item in candidates if item.native and item.cwd == root.cwd]
        evidence = [(_native_holder_evidence(root, item), item) for item in relevant]
        holders = [item for status, item in evidence if status == "holder"]
        if (target is None or len(holders) > 1
                or any(status in ("unavailable", "unstable") for status, _ in evidence)
                or _journal_identity(root.path) != target):
            uncertain.add(root.session_id)
            continue
        if not holders:
            # No holder is healthy absence unless a native candidate explicitly
            # says it belongs to a different thread in this same project.
            if any(arg == "resume" and i + 1 < len(item.argv)
                   and not item.argv[i + 1].startswith("-")
                   and item.argv[i + 1] != root.thread_id
                   for item in relevant for i, arg in enumerate(item.argv)):
                uncertain.add(root.session_id)
            continue
        holder = holders[0]
        other_resume = any(arg == "resume" and i + 1 < len(holder.argv)
                           and not holder.argv[i + 1].startswith("-")
                           and holder.argv[i + 1] != root.thread_id
                           for i, arg in enumerate(holder.argv))
        competing_resume = any(_explicit_resume(item.argv, root.thread_id)
                               and item.pid != holder.pid for item in relevant)
        if other_resume or competing_resume:
            uncertain.add(root.session_id)
            continue
        result[root.session_id] = CodexNavigationProof(
            root, target, holder.pid, holder.executable, holder.create_time,
            holder.argv, holder.tty,
        )
    # A root whose other evidence was conflicting still reserves its holder's
    # pid/tty: no selected root may borrow a shared terminal from it.
    for sid, proof in list(result.items()):
        for other in roots:
            if other.session_id == sid:
                continue
            if any(item.native and (item.pid == proof.pid or item.tty == proof.tty)
                   and _native_holder_evidence(other, item) == "holder"
                   for item in candidates):
                result.pop(sid, None)
                uncertain.add(sid)
                break
    return result


def _child_request_settled(parent, child, requested_at):
    """A matching new turn proves work consumed this accepted request.

    A fast turn may finish before the acknowledgement. Conversely an older
    turn finishing after that acknowledgement says nothing about queued work.
    """
    return bool(requested_at and parent.journal_complete and child.journal_complete
                and child.parent_thread_id == parent.thread_id
                and child.turn_id and child.turn_observed and not child.turn_active
                and child.turn_started_at > requested_at
                and child.turn_completed_id == child.turn_id
                and child.turn_completed_at >= child.turn_started_at
                and not child.stats.question and not child.stats.questions)


def has_unsettled_path_children(record, children):
    """Accepted path/UUID requests are private refusal facts, never fake rows."""
    if not record.journal_complete:
        return True
    children = tuple(children)
    requests = [("path", path, record.path_requests.get(path, 0.0))
                for path in record.spawned_paths.keys() | record.path_events.keys()]
    requests += [("id", child_id, requested_at)
                 for child_id, requested_at in record.child_requests.items()]
    for kind, key, requested_at in requests:
        matches = [child for child in children if child.parent_thread_id == record.thread_id
                   and (child.agent_path if kind == "path" else child.thread_id) == key]
        if len(matches) != 1 or not _child_request_settled(record, matches[0], requested_at):
            return True
    return False


def question_blocks(record, awaiting_answer):
    """A pending question blocks, except a queued async one when answering it."""
    if not (record.stats.question or record.stats.questions):
        return False
    return not (awaiting_answer and record.question_async)


def stopped_turn(record, *, awaiting_answer=False):
    """Affirmative native completion, never silence or an old terminal event.

    `awaiting_answer` is the reply route's reading alone: a completed turn
    whose only pending question is a queued `request_user_input_async` still
    counts as stopped, because the person's next message is that question's
    native answer. Close authority never passes it.
    """
    return bool(record.journal_complete and record.turn_observed and record.turn_id
                and not record.turn_active and record.turn_started_at > 0
                and math.isfinite(record.turn_started_at) and math.isfinite(record.turn_completed_at)
                and record.turn_completed_id == record.turn_id
                and record.turn_completed_at >= record.turn_started_at
                and not question_blocks(record, awaiting_answer))


def refinement_close_observation(roots, root, journal, turn_id, children=(), *, require_stopped=False,
                                 awaiting_answer=False):
    """Fresh exact-holder facts; a receipt, not this proof, grants close authority.

    Parsing again also catches a newer turn/question before the next roster tick.
    This worker never publishes records or mutates the daemon's live state.
    """
    if not turn_id or _journal_identity(root.path) != journal:
        return None
    # A new prompt or question can arrive while process inspection awaits.
    # Pin all journals across parsing AND the final holder scan. A worker may
    # finish after timeout, but it cannot authorize a write from stale text.
    paths = (root.path,) + tuple(path for _tid, path, _parent, _turn in children)
    def revisions():
        try:
            return tuple((st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                         for st in (path.stat() for path in paths))
        except OSError:
            return None
    before = revisions()
    if before is None:
        return None
    finished_children = set()
    parsed_children = []
    for child_id, child_path, parent_id, child_turn in children:
        child = parse_rollout(child_path)
        if (child is None or child.thread_id != child_id
                or child.parent_thread_id != parent_id or child.turn_id != child_turn
                or child.turn_active
                or (require_stopped and not stopped_turn(child))
                or not child.turn_id or child.stats.question or child.stats.questions):
            return None
        finished_children.add(child_id)
        parsed_children.append(child)
    if any(has_unsettled_path_children(child, parsed_children) for child in parsed_children):
        return None
    if any(a.activity not in ("completed", "shutdown", "errored")
           and aid not in finished_children
           for child in parsed_children for aid, a in child.stats.agents.items()):
        return None
    record = parse_rollout(root.path)
    if (record is None or project_title_roots((record,)) != (root,)
            or (require_stopped and not stopped_turn(record, awaiting_answer=awaiting_answer))
            or record.turn_id != turn_id or question_blocks(record, awaiting_answer)
            or has_unsettled_path_children(record, parsed_children)
            or any(a.activity not in ("completed", "shutdown", "errored")
                   and aid not in finished_children
                   for aid, a in record.stats.agents.items())):
        return None
    # A close is authorised off a fresh walk, never the shared reading: the
    # holder must be observed now, and each of the two checks walks again.
    invalidate_process_snapshot()
    proof = resolve_navigation_proofs(roots).get(root.session_id)
    if (proof is None or proof.root != root or proof.journal != journal
            or revisions() != before):
        return None
    return proof


def _add_title_rung(
    assignments: dict[str, str],
    proposals: dict[str, str],
    conflicted: set[str],
) -> None:
    """Add a resolver rung while enforcing one root owner per tty."""
    combined = {**assignments, **proposals}
    owners: dict[str, list[str]] = {}
    for session_id, tty in combined.items():
        owners.setdefault(tty, []).append(session_id)
    collisions = {
        session_id
        for session_ids in owners.values()
        if len(session_ids) > 1
        for session_id in session_ids
    }
    conflicted.update(collisions)
    assignments.clear()
    assignments.update({
        session_id: tty
        for session_id, tty in combined.items()
        if session_id not in conflicted
    })


def resolve_title_ttys(
    title_roots,
    processes=None,
) -> dict[str, str]:
    """Resolve private decorative ttys from exact evidence, never control."""
    roots = tuple(title_roots)
    if not roots:
        return {}
    candidates = _title_processes(processes)
    assignments: dict[str, str] = {}
    conflicted: set[str] = set()

    duplicate_sessions = {
        root.session_id
        for root in roots
        if sum(item.session_id == root.session_id for item in roots) > 1
    }
    conflicted.update(duplicate_sessions)

    holder_ttys: dict[str, list[str]] = {}
    journal_available: dict[str, bool] = {}
    explicit_ttys: dict[str, set[str]] = {}
    for root in roots:
        if root.session_id in conflicted:
            continue
        evidence = [
            (_native_holder_evidence(root, item), item.tty)
            for item in candidates
            if item.native and item.cwd == root.cwd
        ]
        if any(status == "unstable" for status, _tty in evidence):
            conflicted.add(root.session_id)
        holder_ttys[root.session_id] = [
            tty for status, tty in evidence if status == "holder"
        ]
        journal_available[root.session_id] = any(
            status != "unavailable" for status, _tty in evidence
        )
        explicit_ttys[root.session_id] = {
            item.tty for item in candidates
            if item.cwd == root.cwd
            and _explicit_resume(item.argv, root.thread_id)
        }

    journal_proposals: dict[str, str] = {}
    for root in roots:
        if root.session_id in conflicted:
            continue
        holders = holder_ttys.get(root.session_id, [])
        exact = explicit_ttys.get(root.session_id, set())
        if len(holders) != 1:
            continue
        holder_tty = holders[0]
        if len(exact) == 1 and next(iter(exact)) != holder_tty:
            conflicted.add(root.session_id)
            continue
        journal_proposals[root.session_id] = holder_tty
    _add_title_rung(assignments, journal_proposals, conflicted)

    exact_proposals = {}
    for root in roots:
        if root.session_id in assignments or root.session_id in conflicted:
            continue
        holders = holder_ttys.get(root.session_id, [])
        exact = explicit_ttys.get(root.session_id, set())
        if len(holders) != 1 and len(exact) == 1:
            exact_proposals[root.session_id] = next(iter(exact))
    _add_title_rung(assignments, exact_proposals, conflicted)

    by_cwd_roots: dict[str, list[CodexTitleRoot]] = {}
    by_cwd_ttys: dict[str, set[str]] = {}
    for root in roots:
        if root.cwd:
            by_cwd_roots.setdefault(root.cwd, []).append(root)
    for item in candidates:
        by_cwd_ttys.setdefault(item.cwd, set()).add(item.tty)
    cwd_proposals = {}
    for cwd, cwd_roots in by_cwd_roots.items():
        ttys = by_cwd_ttys.get(cwd, set())
        if len(cwd_roots) != 1 or len(ttys) != 1:
            continue
        root = cwd_roots[0]
        if (
            root.session_id not in assignments
            and root.session_id not in conflicted
            and not journal_available.get(root.session_id, False)
        ):
            cwd_proposals[root.session_id] = next(iter(ttys))
    _add_title_rung(assignments, cwd_proposals, conflicted)
    return assignments


def _native_processes(processes=None) -> list[tuple[object, CodexProcessIdentity]]:
    """Fully introspected standalone native Codex processes, fail-closed."""
    if processes is None:
        try:
            processes = psutil.process_iter(
                ["pid", "exe", "cwd", "create_time", "cmdline"])
        except (OSError, PermissionError, psutil.Error):
            return []
    try:
        # A list is read as is (never copied), so the shared scan keeps the
        # identity `_shared_classification` memoises on.
        process_list = processes if isinstance(processes, list) else list(processes)
    except (OSError, PermissionError, psutil.Error):
        return []
    return _shared_classification(process_list, "native", _classify_native)


def _classify_native(process_list, realpaths: dict) -> list[tuple[object, CodexProcessIdentity]]:
    candidates = []
    for proc in process_list:
        try:
            pid = int(_process_field(proc, "pid") or getattr(proc, "pid", 0) or 0)
            executable = _realpath(str(_process_field(proc, "exe") or ""), realpaths)
            cwd = str(_process_field(proc, "cwd") or "")
            created = float(_process_field(proc, "create_time"))
            argv = tuple(str(arg) for arg in (_process_field(proc, "cmdline") or ()))
        except (OSError, PermissionError, TypeError, ValueError, psutil.Error):
            continue
        lower_exe = executable.lower()
        if (
            pid <= 1
            or not executable
            or Path(executable).name != "codex"
            or ".app/contents/" in lower_exe
            or not cwd
            or not argv
            or not _is_tui_invocation(argv)
        ):
            continue
        candidates.append((proc, CodexProcessIdentity(
            pid=pid,
            executable=executable,
            cwd=cwd,
            create_time=created,
            argv=argv,
            match_kind="",
        )))
    return candidates


def _classify_unreadable(process_list, realpaths: dict) -> list[tuple[object, object]]:
    """`(process, cwd)` for every process that cannot be ruled out as a
    Codex — its fields read independently, so a known unrelated executable
    or cwd still rules it out when another field is denied. Native
    candidates pass too; `_observe_root_liveness` skips them."""
    out = []
    for proc in process_list:
        facts = {}
        for field_name in ("exe", "name", "cwd", "cmdline"):
            try:
                facts[field_name] = _process_field(proc, field_name)
            except (OSError, TypeError, ValueError, psutil.Error):
                facts[field_name] = None
        exe = str(facts["exe"] or "")
        name = str(facts["name"] or "")
        if exe and (Path(_realpath(exe, realpaths)).name != "codex"
                    or ".app/contents/" in exe.lower()):
            continue
        if not exe and name and name != "codex":
            continue
        argv = tuple(facts["cmdline"] or ())
        if argv and not _is_tui_invocation(argv):
            continue
        out.append((proc, facts["cwd"]))
    return out


def _fresh_liveness_identity(proc) -> Optional[CodexProcessIdentity]:
    """Recheck native identity without requiring a tty or granting controls."""
    try:
        # psutil caches exe/create_time on each Process, including process_iter
        # instances. Reopening is essential for detecting recycled PIDs.
        if isinstance(proc, _PSUTIL_PROCESS_TYPE):
            proc = psutil.Process(proc.pid)
        return CodexProcessIdentity(
            pid=int(proc.pid), executable=os.path.realpath(str(proc.exe() or "")),
            cwd=str(proc.cwd() or ""), create_time=float(proc.create_time()),
            argv=tuple(str(arg) for arg in (proc.cmdline() or ())), match_kind="",
        )
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
        return None


def _liveness_open_identities(proc, target_paths: set[str]):
    """Keep an unreadable target fd unknown, rather than silently dropping it."""
    try:
        opened = proc.open_files()
    except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
        return None
    identities = []
    for item in opened or ():
        path = getattr(item, "path", item)
        identity = _journal_identity(path)
        if identity is None:
            try:
                if os.path.realpath(os.fspath(path)) in target_paths:
                    return None
            except (OSError, TypeError, ValueError):
                return None
        else:
            identities.append(identity)
    return identities


def _observe_root_liveness(roots, processes, candidates) -> None:
    """One bounded ownership observation per candidate, shared across roots.

    A cwd only scopes inspection. Ownership requires stable native identity
    plus an exact resume or the exact journal held twice. Unknown observations
    cannot become absence; title assignments and control identity are not read.
    """
    targets = {r.session_id: _journal_identity(r.path) for r in roots}
    evidence = {r.session_id: [] for r in roots}
    native = {id(proc): identity for proc, identity in candidates}
    # The processes nothing rules out as a Codex whose fields were denied —
    # classified once per shared scan, like the candidates, rather than
    # every other process re-read and re-resolved on every roster refresh.
    process_list = processes if isinstance(processes, list) else list(processes)
    for proc, cwd in _shared_classification(process_list, "unreadable", _classify_unreadable):
        if id(proc) in native:
            continue
        # A filtered, unreadable Codex candidate is not evidence of death.
        for root in roots:
            if not cwd or cwd == root.cwd:
                evidence[root.session_id].append(None)
    for proc, identity in candidates:
        relevant = [r for r in roots if r.cwd == identity.cwd]
        if not relevant:
            continue
        target_paths = {os.path.realpath(r.path) for r in relevant}
        opened = _liveness_open_identities(proc, target_paths)
        # Revalidate even negative ownership: a recycled PID must not retire
        # a root on the strength of stale process_iter fields.
        stable = _fresh_liveness_identity(proc) == identity
        possible = [r for r in relevant if (
            _explicit_resume(identity.argv, r.thread_id)
            or (targets[r.session_id] is not None and opened is not None
                and targets[r.session_id] in opened)
        )]
        opened_again = (_liveness_open_identities(proc, target_paths)
                        if stable and possible else opened)
        stable = stable and (not possible or _fresh_liveness_identity(proc) == identity)
        for root in relevant:
            target = targets[root.session_id]
            verdict = False
            resumed = _explicit_resume(identity.argv, root.thread_id)
            other_resume = any(arg == "resume" and i + 1 < len(identity.argv)
                               and identity.argv[i + 1] != root.thread_id
                               and not identity.argv[i + 1].startswith("-")
                               for i, arg in enumerate(identity.argv))
            holder = target is not None and opened is not None and target in opened
            if not stable or opened is None or opened_again is None:
                verdict = None
            elif target is None or _journal_identity(root.path) != target:
                verdict = None
            elif holder and other_resume:
                verdict = None
            elif any(item[1:] == target[1:] and item != target for item in opened):
                verdict = None
            elif resumed or holder:
                verdict = True if (not holder or target in opened_again) else None
            evidence[root.session_id].append(verdict)
    for root in roots:
        found = evidence[root.session_id]
        # Conflicting/unstable observations withdraw positive ownership too.
        root.process_seen = None if None in found else any(found)


def attach_process_ids(records: list[CodexRecord], processes=None) -> None:
    """Attach a Codex process only where the rollout→terminal match is unique.

    Explicit `resume <thread-id>` command lines win. Otherwise a cwd is mapped
    only when it contains exactly one root rollout and exactly one Codex process;
    Dark Army never renames a plausible-but-ambiguous VS Code tab. Last comes the
    start-time pairing below, which answers the ordinary case those two miss:
    a project worked in more than once inside the fifteen-minute live window
    has several root rollouts *and* several Codex processes in one folder, so
    `unique_cwd` can never fire there and every row went out with no pid at
    all — which is a session Dark Army cannot attribute a board verb to and cannot
    prove came out of the terminal it opened.

    None of the three is a licence: `matching_process_identity` still admits
    only `explicit_resume` and `unique_cwd`, so a `nearest_start` pid is data
    for attribution and nothing a button may act on.
    """
    roots = [record for record in records if _safe_cli_root(record)]
    if not roots:
        return
    # Materialize exactly once, including caller-supplied iterators. Never let
    # an unavailable scan fall through into the control helper's default scan.
    for record in roots:
        record.pid = None
        record.process_identity = None
        record.process_seen = None
    try:
        observed = process_snapshot_shared() if processes is None else list(processes)
    except (OSError, PermissionError, psutil.Error):
        observed = None
    if observed is None:
        return
    candidates = _native_processes(observed)
    _observe_root_liveness(roots, observed, candidates)

    claimed: set[int] = set()
    for record in roots:
        explicit = [item for item in candidates
                    if item[1].cwd == record.cwd
                    and _explicit_resume(item[1].argv, record.thread_id)
                    and item[1].pid not in claimed]
        if len(explicit) == 1:
            base = explicit[0][1]
            record.process_identity = CodexProcessIdentity(
                **{**base.__dict__, "match_kind": "explicit_resume"})
            record.pid = base.pid
            claimed.add(record.pid)

    by_cwd_records: dict[str, list[CodexRecord]] = {}
    by_cwd_processes: dict[str, list[tuple[object, CodexProcessIdentity]]] = {}
    for record in roots:
        if record.pid is None and record.cwd:
            by_cwd_records.setdefault(record.cwd, []).append(record)
    for item in candidates:
        if item[1].pid not in claimed:
            by_cwd_processes.setdefault(item[1].cwd, []).append(item)
    for cwd, pending in by_cwd_records.items():
        matches = by_cwd_processes.get(cwd) or []
        if len(pending) == 1 and len(matches) == 1:
            base = matches[0][1]
            pending[0].process_identity = CodexProcessIdentity(
                **{**base.__dict__, "match_kind": "unique_cwd"})
            pending[0].pid = base.pid

    _pair_by_start_time(roots, candidates)


def _pair_by_start_time(
    roots: list[CodexRecord],
    candidates: list[tuple[object, CodexProcessIdentity]],
) -> None:
    """Pair the rollouts a folder holds with the processes that wrote them.

    A rollout's first turn begins moments after the process that opened it,
    so within one folder the link is the nearest *preceding* Codex process —
    an ordering rule rather than a nearest-in-either-direction one, because
    two dispatches a minute apart are each other's nearest neighbour and the
    symmetric rule pairs neither.

    Mutual exclusivity is the whole safety property: a process two rollouts
    both choose (one Codex window where somebody started a second thread by
    hand) pairs with neither, and a rollout with no process before it inside
    `NEAREST_START_SECONDS` pairs with nothing. Ambiguity leaves the pid
    None, exactly as it does today.
    """
    taken = {record.pid for record in roots if record.pid is not None}
    pending_by_cwd: dict[str, list[CodexRecord]] = {}
    free_by_cwd: dict[str, list[CodexProcessIdentity]] = {}
    for record in roots:
        if record.pid is None and record.cwd and record.started_at:
            pending_by_cwd.setdefault(record.cwd, []).append(record)
    for _proc, identity in candidates:
        if identity.pid not in taken:
            free_by_cwd.setdefault(identity.cwd, []).append(identity)
    for cwd, pending in pending_by_cwd.items():
        free = free_by_cwd.get(cwd) or []
        if not free:
            continue
        chosen: dict[int, list[tuple[CodexRecord, CodexProcessIdentity]]] = {}
        for record in pending:
            began = float(record.started_at or 0.0)
            before = [item for item in free
                      if 0.0 <= began - item.create_time <= NEAREST_START_SECONDS]
            if not before:
                continue
            best = max(before, key=lambda item: item.create_time)
            chosen.setdefault(best.pid, []).append((record, best))
        for claims in chosen.values():
            if len(claims) != 1:
                continue
            record, identity = claims[0]
            record.process_identity = CodexProcessIdentity(
                **{**identity.__dict__, "match_kind": "nearest_start"})
            record.pid = identity.pid



def matching_process_identity(
    record: CodexRecord,
    identity: CodexProcessIdentity,
    *,
    destructive: bool,
    processes=None,
    records: Optional[list[CodexRecord]] = None,
):
    """Return the uniquely matching live process, or None on any uncertainty."""
    if (
        not _safe_cli_root(record)
        or record.process_identity != identity
        or identity.match_kind not in ("explicit_resume", "unique_cwd")
        or (destructive and identity.match_kind != "explicit_resume")
    ):
        return None
    candidates = _native_processes(processes)
    exact = [item for item in candidates if (
        item[1].pid == identity.pid
        and item[1].executable == identity.executable
        and item[1].cwd == identity.cwd
        and abs(item[1].create_time - identity.create_time) <= 0.01
        and item[1].argv == identity.argv
    )]
    if len(exact) != 1:
        return None
    if identity.match_kind == "explicit_resume":
        matches = [item for item in candidates if (
            item[1].cwd == record.cwd
            and _explicit_resume(item[1].argv, record.thread_id)
        )]
        if len(matches) != 1 or not _explicit_resume(identity.argv, record.thread_id):
            return None
    else:
        matches = [item for item in candidates if item[1].cwd == record.cwd]
        peer_roots = [item for item in (records or [record]) if (
            _safe_cli_root(item) and item.cwd == record.cwd
        )]
        if len(matches) != 1 or len(peer_roots) != 1:
            return None
    return exact[0][0]


def validate_process_identity(
    record: CodexRecord,
    identity: CodexProcessIdentity,
    *,
    destructive: bool,
    processes=None,
    records: Optional[list[CodexRecord]] = None,
) -> bool:
    """Pure yes/no ownership contract used by Jump and destructive Stop."""
    return matching_process_identity(
        record, identity, destructive=destructive,
        processes=processes, records=records,
    ) is not None


def _load_usage_limits(
    root: Optional[Path] = None,
    *, now: Optional[float] = None,
    grace: float = USAGE_GRACE_SECONDS,
) -> list[tuple[float, list[dict]]]:
    """Read account bars through a worker-only, mtime+size bounded cache."""
    root = SESSIONS_DIR if root is None else root
    clock = time.time() if now is None else now
    try:
        paths = sorted(
            root.glob("*/*/*/rollout-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:MAX_FILES]
    except OSError:
        return []

    snapshots = []
    live_paths = set(paths)
    # Parsing stays under this usage-only lock. Two worker pools may request the
    # same snapshot concurrently; the second must reuse the first parse rather
    # than race a second 90 MB journal pass. The daemon-loop `_CACHE` is never
    # read or mutated here.
    with _USAGE_CACHE_LOCK:
        for cached_path in list(_USAGE_CACHE):
            if cached_path not in live_paths:
                _USAGE_CACHE.pop(cached_path, None)
        for path in paths:
            try:
                stat = path.stat()
                if clock - stat.st_mtime > grace:
                    continue
            except OSError:
                continue
            key = (stat.st_mtime_ns, stat.st_size)
            cached = _USAGE_CACHE.get(path)
            if cached is None or cached[:2] != key:
                record = parse_rollout(path)
                last_event = record.last_event if record is not None else stat.st_mtime
                bars = deepcopy(record.limit_bars) if record is not None else []
                cached = (*key, last_event, bars)
                _USAGE_CACHE[path] = cached
            snapshots.append((cached[2], deepcopy(cached[3])))
    return snapshots


def _stamp_codex_bars(bars: list, clock: float) -> list[dict]:
    stamped = deepcopy(bars)
    for bar in stamped:
        resets = bar.get("resets_at")
        bar["stale"] = isinstance(resets, (int, float)) and resets <= clock
    return stamped


def usage_snapshot(records: Optional[list[CodexRecord]] = None,
                   *, now: Optional[float] = None) -> dict:
    """The freshest Codex account windows in the same shape as `/api/usage`.

    Journals are preferred. When none still carry bars — a quiet terminal,
    a grace window that has closed — the last reading we stored is served
    instead, so the chip stays put. Tests that pass `records` do not
    touch that hold: they are asserting the journal path, not the disk.
    """
    persist = records is None
    if persist:
        candidates = [item for item in _load_usage_limits(now=now) if item[1]]
    else:
        candidates = [
            (record.last_event, record.limit_bars)
            for record in records if record.limit_bars
        ]
    clock = time.time() if now is None else now
    if not candidates:
        if persist:
            held = usage_hold.load("codex")
            bars = held.get("bars") if isinstance(held.get("bars"), list) else []
            if bars:
                return {"available": True, "provider": "codex",
                        "bars": _stamp_codex_bars(bars, clock),
                        "fetched_at": held.get("fetched_at")}
        return {"available": False, "bars": []}
    freshest = max(candidates, key=lambda item: item[0])
    bars = _stamp_codex_bars(freshest[1], clock)
    if persist:
        usage_hold.save("codex", {
            "available": True,
            "provider": "codex",
            "bars": deepcopy(freshest[1]),
            "fetched_at": freshest[0],
        })
    return {"available": True, "provider": "codex", "bars": bars,
            "fetched_at": freshest[0]}


def record_for_hook_session(records, sid: str):
    """Find exactly one root for a hook's session or thread id.

    A shared cwd is insufficient proof of identity. Ambiguous or child matches
    never acquire a permission row.
    """
    matches = [record for record in records.values()
               if sid and sid in (record.session_id, record.thread_id)]
    if len(matches) != 1 or matches[0].is_child:
        return None
    return matches[0]
