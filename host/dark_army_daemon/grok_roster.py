"""Read-only view of live Grok sessions.

Grok writes ``~/.grok/active_sessions.json`` — session_id, pid, cwd, opened_at —
and per-session files under ``~/.grok/sessions/<urlencoded-cwd>/<session-id>/``:

    summary.json     title, model, branch, cwd, timestamps (turn end)
    signals.json     context %, counters, line counts (turn end)
    updates.jsonl    per-turn usage ledger — tokens, cost, model calls (live);
                     mid-turn ``_meta.totalTokens`` is the live context fill
    events.jsonl     tool_started / tool_completed (live)
    chat_history.jsonl  conversation — spoken assistant turns (live)

Nothing here writes to Grok's state directory. The listing is a file read, not
a CLI fork — the opposite of ``claude agents --json``.
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

from .ai_title import (
    _SYNTHETIC_PROMPT_PREFIXES,
    _first_text,
    _strip_leading_refs,
)
from .session_stats import SessionStats
from .grok_usage import (
    DEFAULT_CONTEXT_WINDOW,
    GrokUsage,
    UsageCache,
    iso_epoch,
    read_jsonl_delta,
)
from .grok_events import EventsCache, GrokEvents
from .grok_chat import ChatCache, GrokChat

logger = logging.getLogger("dark-army")

GROK_HOME = Path.home() / ".grok"
ACTIVE_SESSIONS_PATH = GROK_HOME / "active_sessions.json"
SESSIONS_DIR = GROK_HOME / "sessions"

# Spawn result: "Subagent started in background. subagent_id: <uuid> type: bc-planner"
# Poll result (one child):  "=== Task <uuid> === ... Status: running|completed"
# Poll result (wait_all):   "--- Task <uuid> [running] ---"
#                           "--- Task <uuid> [completed] ---"
_SPAWN_BANNER = "Subagent started"
# How far before `subagent_id:` the harness banner may sit. Real spawn is
# the banner, a newline, then the id (~35 chars). A file-read of this
# module quoting the pattern has the id and no banner in that window.
_UUID = (
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_SPAWN_ID_RE = re.compile(
    rf"\bsubagent_id:\s*{_UUID}",
    re.IGNORECASE,
)
_TASK_STATUS_RE = re.compile(
    rf"=== Task {_UUID} ==="
    r".*?Status:\s*(\w+)",
    re.IGNORECASE | re.DOTALL,
)
_TASK_BRACKET_RE = re.compile(
    rf"(?:===|---)\s*Task\s+{_UUID}\s*\[(\w+)\]",
    re.IGNORECASE,
)
_LIVE_STATUS = "running"
_LIVE_CACHE_MAX = 256
_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE,
)


@dataclass
class _LiveScan:
    """Where a chat_history parse got to, so the next one resumes there.

    `chat_history.jsonl` is append-only and reaches megabytes on a long
    session, while its mtime moves on **every assistant chunk** — so an
    mtime-keyed cache over a whole-file read missed on essentially every
    call, and this is reached from the agents snapshot several times a
    second. Keeping the accumulated status map and a byte offset makes the
    steady-state cost the new bytes only.

    `offset` stops at the last newline, never mid-record: the file is being
    appended to as we read it, and half a JSON line is not a record yet. It
    is re-read whole if the file shrinks or is replaced (`/clear`, a rotate),
    which is the only way the accumulated map can be wrong.
    """
    mtime: float
    size: int
    offset: int
    status: dict
    order: list
    result: Optional[list]


# path -> _LiveScan
_live_cache: "OrderedDict[str, _LiveScan]" = OrderedDict()

# Same guard as session_registry: a listing of live sessions, not history.
MAX_SESSIONS = 256


@dataclass(frozen=True)
class GrokRecord:
    """One row of ~/.grok/active_sessions.json."""

    session_id: str
    pid: Optional[int] = None
    cwd: str = ""
    opened_at: Optional[float] = None

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else ""


# One ISO→epoch parser for every Grok reader; this module's copy retired.
_epoch = iso_epoch


def _load_json(path: Path) -> Optional[dict | list]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data


def load_active(path: Optional[Path] = None) -> Optional[list[GrokRecord]]:
    """Parse the live-session roster. None if missing, unreadable, or not a list.

    A failed read is not an empty fleet — the caller must not treat it as
    everyone having left.
    """
    raw = _load_json(path if path is not None else ACTIVE_SESSIONS_PATH)
    if not isinstance(raw, list):
        return None
    out: list[GrokRecord] = []
    for item in raw[:MAX_SESSIONS]:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("session_id") or "")
        if not sid:
            continue
        pid = item.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            pid = None
        out.append(GrokRecord(
            session_id=sid,
            pid=pid,
            cwd=str(item.get("cwd") or ""),
            opened_at=_epoch(item.get("opened_at")),
        ))
    return out


def read_active(path: Optional[Path] = None) -> list[GrokRecord]:
    """Parse the live-session roster. Empty on any problem — a missing file is
    a machine that has never run Grok, not an error."""
    return load_active(path) or []


def session_dir(session_id: str, cwd: str = "",
                sessions_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate ``~/.grok/sessions/<encoded-cwd>/<session-id>/``.

    The cwd encoding is URL-quote with no safe characters (Grok's rule). When
    cwd is known we go straight there; otherwise we glob, which is how a hook
    event that never carried cwd still finds its title.
    """
    if not session_id:
        return None
    if sessions_dir is None:
        sessions_dir = SESSIONS_DIR
    if cwd:
        direct = sessions_dir / quote(cwd, safe="") / session_id
        if direct.is_dir():
            return direct
    try:
        matches = list(sessions_dir.glob(f"*/{session_id}"))
    except OSError:
        return None
    dirs = [p for p in matches if p.is_dir()]
    if not dirs:
        return None
    try:
        return max(dirs, key=lambda p: p.stat().st_mtime)
    except OSError:
        return dirs[0]


#: The files Grok appends to while a turn runs. A session whose tab has
#: gone still writes these under the shared leader — the turn does not stop
#: with the terminal — so their freshness is the one witness a headless
#: session has.
ACTIVITY_FILES = ("events.jsonl", "updates.jsonl", "chat_history.jsonl")


def last_activity(session_id: str, cwd: str = "",
                  children: tuple[str, ...] | list[str] = (),
                  sessions_dir: Optional[Path] = None) -> Optional[float]:
    """The newest write under this session's directory, or any live child's.

    Epoch seconds, or None when no directory is there to read. A child runs
    in a session directory of its own beside the parent's, and a parent
    blocked on ``get_command_or_subagent_output`` writes nothing itself for
    as long as the child works — so the child's files count for the parent.
    """
    newest: Optional[float] = None
    for sid in (session_id, *children):
        directory = session_dir(sid, cwd, sessions_dir)
        if directory is None:
            continue
        for name in ACTIVITY_FILES:
            try:
                mtime = (directory / name).stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def read_summary(session_id: str, cwd: str = "",
                 sessions_dir: Optional[Path] = None) -> dict:
    """``summary.json`` as a dict, or {} if it is missing or unreadable."""
    directory = session_dir(session_id, cwd, sessions_dir)
    if directory is None:
        return {}
    raw = _load_json(directory / "summary.json")
    return raw if isinstance(raw, dict) else {}


def read_signals(session_id: str, cwd: str = "",
                 sessions_dir: Optional[Path] = None) -> dict:
    directory = session_dir(session_id, cwd, sessions_dir)
    if directory is None:
        return {}
    raw = _load_json(directory / "signals.json")
    return raw if isinstance(raw, dict) else {}


def _chat_text(obj: dict) -> str:
    content = obj.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _read_new_lines(path: Path, offset: int) -> tuple[str, int]:
    """Complete records appended since `offset`, and the offset they end at.

    Routed through ``grok_usage.read_jsonl_delta``, which holds a half-written
    trailing line for the next pass — and, unlike the local cut-at-last-newline
    this replaces, also *takes* a trailing line that already parses as JSON
    (``tail_is_complete``): a finished record still waiting for its newline.
    """
    lines: list[str] = []
    try:
        offset = read_jsonl_delta(path, offset, lines.append)
    except OSError:
        return "", offset
    if not lines:
        return "", offset
    return "\n".join(lines) + "\n", offset


def _record_task_status(status: dict, order: list, sid: str, state: str) -> None:
    if sid not in status:
        order.append(sid)
    status[sid] = state


def _scan_live_subagents(text: str, status: dict, order: list) -> None:
    """Fold one run of chat_history lines into `status`/`order`, in place."""
    for line in text.splitlines():
        # Cheap reject: spawn records, the single-wait Status: form, and
        # the wait_all `--- Task <id> [completed] ---` form. A completion
        # that carries neither `subagent_id` nor `Status:` used to be
        # skipped, so parallel children stayed live after they finished.
        if (
            "subagent_id" not in line
            and "Status:" not in line
            and "--- Task" not in line
            and "=== Task" not in line
        ):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        blob = _chat_text(obj)
        if not blob:
            continue
        for match in _SPAWN_ID_RE.finditer(blob):
            sid = match.group(1)
            if sid in status:
                continue
            # Quoted source and tool results that *read* this file carry
            # `subagent_id: <uuid>` without the harness banner. A real spawn
            # always has it. Without this, opening grok_roster.py in a
            # session minted a ghost child that kept the parent working.
            window = blob[max(0, match.start() - 120):match.start()]
            if _SPAWN_BANNER not in window:
                continue
            order.append(sid)
            status[sid] = _LIVE_STATUS
        for match in _TASK_STATUS_RE.finditer(blob):
            _record_task_status(
                status, order, match.group(1), match.group(2).lower()
            )
        for match in _TASK_BRACKET_RE.finditer(blob):
            _record_task_status(
                status, order, match.group(1), match.group(2).lower()
            )


def _live_from_status(status: dict, order: list) -> Optional[list]:
    """The public answer: ids still running, or None if none were ever spawned.

    None means "this file has no spawn records" — the hook set must stay.
    An empty list means every spawned child has finished.
    """
    if not status:
        return None
    return [sid for sid in order if status.get(sid) == _LIVE_STATUS]


def live_subagent_ids(
    session_id: str, cwd: str = "",
    sessions_dir: Optional[Path] = None,
) -> Optional[list[str]]:
    """Live Grok children of this parent, from ``chat_history.jsonl``.

    Hooks key Grok children by type and fire SubagentStop on the child
    session, so the in-memory set is a lifetime of types. The poll text
    is the parent's own record of who is still running.
    """
    directory = session_dir(session_id, cwd, sessions_dir)
    if directory is None:
        return None
    path = directory / "chat_history.jsonl"
    try:
        st = path.stat()
    except OSError:
        return None
    mtime, size = st.st_mtime, st.st_size
    key = str(path)
    scan = _live_cache.get(key)
    if scan is not None and scan.mtime == mtime and scan.size == size:
        _live_cache.move_to_end(key)
        return scan.result
    if scan is None or size < scan.offset:
        # First look, or the file was truncated or replaced — everything we
        # accumulated is about a file that no longer exists under this name.
        scan = _LiveScan(mtime=0.0, size=0, offset=0, status={}, order=[], result=None)
    text, offset = _read_new_lines(path, scan.offset)
    if text:
        _scan_live_subagents(text, scan.status, scan.order)
    scan.mtime, scan.size, scan.offset = mtime, size, offset
    scan.result = _live_from_status(scan.status, scan.order)
    _live_cache[key] = scan
    _live_cache.move_to_end(key)
    while len(_live_cache) > _LIVE_CACHE_MAX:
        _live_cache.popitem(last=False)
    return scan.result


def title_of(summary: dict) -> str:
    """Best human name Grok itself has written. ``generated_title`` is what
    `/rename` and auto-titling both land in; ``session_summary`` is the longer
    description and is the fallback when the title has not been minted yet."""
    for key in ("generated_title", "session_summary"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def first_prompt_from_dir(directory: Path) -> str:
    """The instruction the person opened with, when Grok never minted a title.

    ``generated_title`` / ``session_summary`` are the real names and are
    preferred by the caller. This is the fallback for a session that has
    work in it and no title — not for the empty ghosts that only ever
    carried a ``synthetic_reason`` skill dump.
    """
    path = directory / "chat_history.jsonl"
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return ""
    with handle:
        for line in handle:
            if '"user"' not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            if obj.get("synthetic_reason"):
                continue
            content = obj.get("content")
            if content is None:
                message = obj.get("message")
                content = message.get("content") if isinstance(message, dict) else ""
            text = " ".join(_first_text(content).split())
            if not text:
                continue
            query = _USER_QUERY_RE.search(text)
            if query:
                text = " ".join(query.group(1).split())
            elif text.startswith("<user_info>"):
                continue
            if not text or text.startswith(_SYNTHETIC_PROMPT_PREFIXES):
                continue
            text = _strip_leading_refs(text)
            if text:
                return text
    return ""


def first_prompt(session_id: str, cwd: str = "",
                 sessions_dir: Optional[Path] = None) -> str:
    """Opening user prompt for a Grok session, or ''."""
    directory = session_dir(session_id, cwd, sessions_dir)
    if directory is None:
        return ""
    return first_prompt_from_dir(directory)


def branch_of(summary: dict) -> str:
    value = summary.get("head_branch")
    return value.strip() if isinstance(value, str) else ""


def cwd_of(summary: dict) -> str:
    info = summary.get("info")
    if isinstance(info, dict) and isinstance(info.get("cwd"), str):
        return info["cwd"]
    if isinstance(summary.get("git_root_dir"), str):
        return summary["git_root_dir"]
    return ""


def folder_for_session(session_id: str, cwd: str = "",
                       sessions_dir: Optional[Path] = None) -> str:
    """The working directory Grok stored for this session, or "".

    MCP processes under ``grok agent leader`` inherit the leader's cwd, which
    is not the session's. The on-disk folder is the honest one:
    ``~/.grok/sessions/<urlencoded-cwd>/<session-id>/``. Prefer ``summary.json``
    when it carries a cwd; otherwise unquote the parent directory name.
    """
    directory = session_dir(session_id, cwd, sessions_dir)
    if directory is None:
        return ""
    from_summary = cwd_of(read_summary(session_id, cwd, sessions_dir))
    if from_summary:
        return from_summary
    try:
        return unquote(directory.parent.name)
    except (TypeError, ValueError):
        return ""


def stats_from(signals: dict, summary: dict, *,
               ended_at: Optional[float] = None) -> SessionStats:
    """Map Grok's signals + summary onto the SessionStats the snapshot already
    ships. Fields we do not have (cache-split tokens, per-tool counts) stay
    zero / empty rather than being invented."""
    stats = SessionStats()
    model = ""
    if isinstance(summary.get("current_model_id"), str):
        model = summary["current_model_id"]
    elif isinstance(signals.get("primaryModelId"), str):
        model = signals["primaryModelId"]
    used = signals.get("modelsUsed")
    models = [m for m in used if isinstance(m, str) and m] if isinstance(used, list) else []
    if model and model not in models:
        models.insert(0, model)
    stats.model = model or (models[0] if models else None)
    stats.models = models
    if isinstance(summary.get("reasoning_effort"), str):
        stats.effort = summary["reasoning_effort"]
    if isinstance(signals.get("assistantMessageCount"), int):
        stats.assistant_messages = signals["assistantMessageCount"]
    if isinstance(signals.get("userMessageCount"), int):
        stats.user_prompts = signals["userMessageCount"]
    if isinstance(signals.get("toolCallCount"), int) and signals["toolCallCount"] > 0:
        # We have a total, not a per-tool breakdown. One bucket keeps
        # total_tool_calls honest without inventing names.
        stats.tool_counts = {"tools": int(signals["toolCallCount"])}
    files = signals.get("totalFilesTouched") or signals.get("agentFilesTouched")
    if isinstance(files, int) and files >= 0:
        stats.files_touched = files
    stats.git_branch = branch_of(summary)
    stats.cwd = cwd_of(summary)
    duration = signals.get("sessionDurationSeconds")
    if isinstance(duration, (int, float)) and duration > 0:
        # SessionStats.duration_seconds is first→last of the transcript. Grok
        # already measured wall time; synthesise a span so the property works.
        # The span ends when the signals file was last written (``ended_at``),
        # never at *now*: `first_ts` is what `daemon._session_started_at`
        # publishes as the row's `started_at`, a fixed point that is news
        # once — and a finished Grok session's duration is frozen while the
        # clock is not, so a span anchored to now walked forward on every
        # build, every frame was news, and the SSE quiet floor never engaged
        # (three finished Grok rows: seven full frames in ten seconds, the
        # panel at 15 % CPU — 21 Sep 2026). Only a caller with no file at all
        # falls back to the clock.
        end = (datetime.fromtimestamp(float(ended_at), tz=timezone.utc)
               if ended_at else datetime.now(timezone.utc))
        start = datetime.fromtimestamp(end.timestamp() - float(duration), tz=timezone.utc)
        stats.first_ts = start
        stats.last_ts = end
    return stats


def metrics_from(signals: dict, summary: dict) -> dict:
    """Statusline-shaped metrics from Grok files, so the panel's context cell
    has something to draw. Absent fields stay out — same contract as
    flatten_statusline, where missing and zero are different."""
    out: dict = {}
    model = summary.get("current_model_id") or signals.get("primaryModelId")
    if isinstance(model, str) and model:
        out["model_id"] = model
        out["model_name"] = model
    effort = summary.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        out["effort"] = effort
    ctx = signals.get("contextWindowUsage")
    if isinstance(ctx, (int, float)) and not isinstance(ctx, bool):
        out["ctx_used_pct"] = ctx
    size = signals.get("contextWindowTokens")
    if isinstance(size, int) and size > 0:
        out["ctx_size"] = size
    used = signals.get("contextTokensUsed")
    if isinstance(used, int) and used >= 0:
        out["ctx_input_tokens"] = used
    cwd = cwd_of(summary)
    if cwd:
        out["cwd"] = cwd
    return out


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _overlay_usage(stats: SessionStats, metrics: dict, usage: GrokUsage) -> None:
    if usage.turns <= 0:
        return
    stats.input_tokens = usage.input_tokens
    stats.output_tokens = usage.output_tokens
    stats.cache_read_tokens = usage.cache_read_tokens
    stats.cache_creation_tokens = usage.cache_creation_tokens
    if usage.models:
        extras = [m for m in usage.models if m not in stats.models]
        stats.models = list(stats.models) + extras
    if usage.cost_usd is not None:
        # Omitted entirely when unknown — never 0.0. The panel treats a
        # missing key as "not reported yet"; a zero is a free session.
        metrics["cost_usd"] = usage.cost_usd
    if usage.api_duration_ms:
        metrics["api_duration_ms"] = usage.api_duration_ms
    if stats.duration_seconds <= 0 and usage.first_ts and usage.last_ts:
        stats.first_ts = datetime.fromtimestamp(usage.first_ts, tz=timezone.utc)
        stats.last_ts = datetime.fromtimestamp(usage.last_ts, tz=timezone.utc)
    if stats.assistant_messages <= 0 and usage.turns:
        stats.assistant_messages = usage.turns


def _overlay_events(stats: SessionStats, events: GrokEvents) -> None:
    if events.tool_counts:
        # Replaces the single ``{"tools": N}`` bucket from signals.json.
        # The displayed name stays the one Grok wrote.
        stats.tool_counts = dict(events.tool_counts)


def _overlay_chat(stats: SessionStats, chat: GrokChat) -> None:
    stats.last_text = chat.last_text
    stats.last_summary = chat.last_summary
    stats.last_actions = list(chat.last_actions)
    stats.last_report = chat.last_report
    # Grok writes the asking turn to chat_history while the dialog is up,
    # so this is a timely source — unlike Claude, whose transcript only
    # carries the question alongside its answer. Empty once the matching
    # tool_result lands, even if later assistant records have already
    # arrived for other tools on the same turn.
    stats.question = dict(chat.question)
    stats.questions = list(chat.questions)


def _overlay_signal_counters(metrics: dict, signals: dict) -> None:
    added = signals.get("agentLinesAdded")
    if isinstance(added, int) and not isinstance(added, bool):
        metrics["lines_added"] = added
    removed = signals.get("agentLinesRemoved")
    if isinstance(removed, int) and not isinstance(removed, bool):
        metrics["lines_removed"] = removed
    doom = signals.get("doomLoopRecoveryAttempts")
    if isinstance(doom, int) and not isinstance(doom, bool):
        metrics["doom_loop_attempts"] = doom
    cancels = signals.get("consecutiveCancellations")
    if isinstance(cancels, int) and not isinstance(cancels, bool):
        metrics["consecutive_cancellations"] = cancels
    retries = signals.get("editAndRetryCount")
    if isinstance(retries, int) and not isinstance(retries, bool):
        metrics["edit_and_retry_count"] = retries
    if signals.get("hasReverted") is True:
        metrics["has_reverted"] = True


def _overlay_live_context(
    metrics: dict,
    usage: GrokUsage,
    signals_mtime: Optional[float],
    updates_mtime: Optional[float],
) -> None:
    """Prefer signals.json when it is the fresher reading; otherwise derive.

    ``signals.json`` is Grok's own context fill, written at turn end.
    ``updates.jsonl`` grows during the turn. When the ledger is newer (or
    signals never arrived) prefer ``_meta.totalTokens`` — that is the live
    window, and it is the only figure a first-turn session has. Fall back
    to the last completed turn's mean prompt (``last_prompt_tokens``) for
    files that predate the _meta stamp.
    """
    have_signals = "ctx_used_pct" in metrics
    signals_is_fresher = (
        have_signals
        and signals_mtime is not None
        and (updates_mtime is None or signals_mtime >= updates_mtime)
    )
    if signals_is_fresher:
        return
    prompt = usage.last_total_tokens or usage.last_prompt_tokens
    if prompt <= 0:
        return
    window = metrics.get("ctx_size")
    if not isinstance(window, int) or window <= 0:
        window = DEFAULT_CONTEXT_WINDOW
    pct = 100.0 * prompt / window
    metrics["ctx_used_pct"] = min(100.0, max(0.0, pct))
    metrics["ctx_input_tokens"] = prompt
    metrics["ctx_size"] = window


def _overlay_billing(metrics: dict, billing: Optional[dict]) -> None:
    if not billing or billing.get("stale"):
        return
    percent = billing.get("percent")
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return
    # Same key Claude rows use for the account window, so the existing
    # budget rule can see it. evaluate_global groups by provider so a
    # weekly Grok figure is never max()'d against Claude's 5h window.
    metrics["five_hour_pct"] = float(percent)
    resets = billing.get("resets_at")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool):
        metrics["five_hour_resets_at"] = float(resets)
    cycle = billing.get("cycle")
    if isinstance(cycle, str) and cycle:
        metrics["budget_cycle"] = cycle


def _signals_mtime(directory: Optional[Path]) -> Optional[float]:
    """When Grok last wrote the session's signals — the fixed end of the
    synthesised span in `stats_from`. None without a file."""
    if directory is None:
        return None
    try:
        return (directory / "signals.json").stat().st_mtime
    except OSError:
        return None


def enrich(
    session_id: str,
    cwd: str = "",
    sessions_dir: Optional[Path] = None,
    *,
    usage_cache: Optional[UsageCache] = None,
    events_cache: Optional[EventsCache] = None,
    chat_cache: Optional[ChatCache] = None,
    billing: Optional[dict] = None,
) -> tuple[SessionStats, dict]:
    """Compose summary + signals + the live files into ``(stats, metrics)``.

    The file readers stay separately testable; this is the one call the
    daemon makes. Caches are the caller's — one per daemon, so a tick costs
    the appended bytes, not the whole file. ``billing`` is the already-fetched
    account snapshot; we do not fetch here.
    """
    summary = read_summary(session_id, cwd, sessions_dir)
    signals = read_signals(session_id, cwd, sessions_dir)
    directory = session_dir(session_id, cwd, sessions_dir)
    stats = stats_from(signals, summary, ended_at=_signals_mtime(directory))
    metrics = metrics_from(signals, summary)

    updates = directory / "updates.jsonl" if directory is not None else None
    events_path = directory / "events.jsonl" if directory is not None else None

    usage = GrokUsage()
    if updates is not None:
        cache = usage_cache if usage_cache is not None else UsageCache()
        usage = cache.get(str(updates))
        _overlay_usage(stats, metrics, usage)

    if events_path is not None:
        cache = events_cache if events_cache is not None else EventsCache()
        _overlay_events(stats, cache.get(str(events_path)))

    if directory is not None:
        cache = chat_cache if chat_cache is not None else ChatCache()
        _overlay_chat(stats, cache.get(str(directory / "chat_history.jsonl")))

    _overlay_signal_counters(metrics, signals)
    _overlay_live_context(
        metrics, usage,
        _mtime(directory / "signals.json") if directory is not None else None,
        _mtime(updates) if updates is not None else None,
    )
    _overlay_billing(metrics, billing)
    return stats, metrics
