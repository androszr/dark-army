"""Backfill: Claude Code transcripts → the history database.

The live writer only knows what happened while Dark Army was running. This scanner
reaches back through `~/.claude/projects/**/*.jsonl` for per-turn token detail and
for every context compaction, including sessions from before Dark Army was ever
installed — and it is what keeps that detail after Claude Code prunes the
transcripts themselves.

Four correctness rules, each of which is a way to get the bill wrong:

* **One turn per `message.id`, last record wins.** Claude Code writes several
  records for a single API response and only the final one carries the settled
  usage numbers. Summing them multiplies the cost by however many records the
  response happened to produce.
* **Re-scanning must be free of side effects.** Turns go in with INSERT OR
  IGNORE against a unique index on message_id, so a file read twice contributes
  once.
* **Cost is computed per turn, then summed.** A session that switches models
  mid-way cannot be priced at one rate — and per-model grouping is the only way
  the promotional-rate window can apply to the right days.
* **Subagents are separate files, and they are not optional.** A subagent's
  turns live at `<project>/<session-id>/subagents/agent-<id>.jsonl`, one level
  deeper than the session transcript, and they are *not* copied into the parent —
  so a scan that only globs `*/*.jsonl` under-counts every total by the whole
  subagent workload. Locally that is 1779 files against 963 session transcripts.

Attribution comes free with the same records. Claude Code stamps each `assistant`
record with what caused it — `attributionSkill`, `attributionAgent`,
`attributionPlugin`, `attributionMcpServer` / `attributionMcpTool` — on the very
record that carries `message.usage`, which is what makes "which skill spent my
week" a GROUP BY rather than a guess.

Incremental by `(path, mtime, line count)`: an unchanged file is skipped on one
stat, and a grown file is read from where the last scan stopped. Transcripts are
append-only, which is what makes the line offset safe.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import pricing
from .history import HistoryStore, local_day
from .paths import TITLES_PATH
from .session_stats import CLAUDE_PROJECTS_DIR

# `ai_title.history_title` is imported inside `_apply_missing_titles`:
# ai_title imports `iter_records` from here, and a top-level import in both
# directions is a cycle that breaks whichever module loads second.

logger = logging.getLogger("dark-army.scan")


def iter_records(source, marker: str, rtype: str = "",
                 type_field: str = "type"):
    """JSONL records that carry `marker` and whose `type_field` is `rtype`.

    The one loop behind `parse_turns`, `parse_compactions`,
    `ai_title.read_ai_title` and `ai_title._first_user_prompt`, which each
    re-implemented it: substring prefilter (most lines are not the record
    sought, and `json.loads` is the expensive step), then parse, then the
    type check. `source` is any iterable of lines — a list, or an open file.

    The partial-last-line rule rides along for free: a half-written trailing
    line of a live file fails `json.loads` and is skipped, while a finished
    record still waiting for its newline parses whole and is yielded — the
    same contract as `grok_usage.tail_is_complete`. Callers that track a scan
    position must still hold the torn tail out of the *count* themselves
    (see `_scan_file`); this generator records nothing.

    An empty `rtype` disables the type check rather than matching a record
    whose type is the empty string.
    """
    for line in source:
        if marker not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if rtype and obj.get(type_field) != rtype:
            continue
        yield obj

# Records that carry usage. Everything else in a transcript is skipped without
# being parsed — see the prefilter in _scan_file.
_USAGE_MARKER = '"usage"'
# The one record that says a context window was compacted. Its own marker,
# because compaction records carry no usage and the prefilter above drops them.
_COMPACT_MARKER = '"compact_boundary"'

# mtime is compared exactly, not within a tolerance. SQLite stores it as a
# double and returns it bit-identical (verified), so there is no float noise to
# absorb — and a tolerance is not free: a transcript appended within the window
# of its own scan would be skipped, and if it never changed again those turns
# would never be read. A spurious re-read costs one file read and inserts
# nothing (the message_id index makes it idempotent); a missed append is
# permanent.


def _project_from_dir(name: str) -> str:
    """Last-resort project name from a transcript directory like
    `-Users-x-Documents-my-repo`.

    Claude Code encodes the cwd by replacing every separator with a dash, which
    makes the encoding ambiguous: `my-repo` and `my/repo` produce the same string.
    Splitting on the last dash therefore reads "shop-front" as "front". Only used
    when the transcript carries no `cwd` of its own — which it almost always
    does, and which is exact."""
    return name.rstrip("-").rsplit("-", 1)[-1] or name


def _parse_ts(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _first_tool_name(content) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            return str(item.get("name") or "")
    return ""


# Transcript field → turn column. Claude Code's own /usage screen reads exactly
# these, so the set is copied rather than invented: matching it is what lets the
# two surfaces be compared when a number looks wrong.
_ATTRIBUTION_FIELDS = {
    "attributionSkill": "attr_skill",
    "attributionAgent": "attr_agent",
    "attributionPlugin": "attr_plugin",
    "attributionMcpServer": "attr_mcp_server",
    "attributionMcpTool": "attr_mcp_tool",
}
# Agent attribution is written either bare (`general-purpose`, which is what
# local transcripts hold) or namespaced (`agent:builtin:Explore`). Stripping the
# namespace here keeps one agent from appearing as two rows.
_AGENT_PREFIXES = ("agent:builtin:", "agent:custom:", "agent:")


def _attribution(obj: dict) -> dict:
    """Pull the attribution stamps off one assistant record.

    Absent stays absent: a turn with no skill is `None`, not `""`, so the reports
    can group on the column without an unattributed bucket masquerading as a
    skill whose name is the empty string."""
    out: dict[str, Optional[str]] = {}
    for field, column in _ATTRIBUTION_FIELDS.items():
        value = obj.get(field)
        value = value.strip() if isinstance(value, str) else ""
        if column == "attr_agent":
            for prefix in _AGENT_PREFIXES:
                if value.startswith(prefix):
                    value = value[len(prefix):]
                    break
        out[column] = value or None
    return out


def parse_turns(lines) -> list[dict]:
    """Extract deduplicated turns from transcript lines.

    Last-record-wins is applied here, before anything reaches the database: a
    dict keyed by message id, overwritten as later records for the same response
    arrive. Records with no id cannot be deduplicated and are all kept."""
    by_id: dict[str, dict] = {}
    anonymous: list[dict] = []

    for obj in iter_records(lines, _USAGE_MARKER, "assistant"):
        message = obj.get("message")
        message = message if isinstance(message, dict) else {}
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        cache_creation = usage.get("cache_creation")
        cache_creation = cache_creation if isinstance(cache_creation, dict) else {}
        write_5m = _int(cache_creation.get("ephemeral_5m_input_tokens"))
        write_1h = _int(cache_creation.get("ephemeral_1h_input_tokens"))
        total_write = _int(usage.get("cache_creation_input_tokens"))
        if not (write_5m or write_1h):
            # Older transcripts report only the total. Attribute it to the
            # 5-minute TTL — the cheaper of the two, so an unknown split cannot
            # inflate the estimate.
            write_5m = total_write

        turn = {
            "session_id": obj.get("sessionId") or "",
            "ts": _parse_ts(obj.get("timestamp")),
            "message_id": str(message.get("id") or ""),
            "model": message.get("model") or "",
            "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")),
            "cache_creation": total_write or (write_5m + write_1h),
            "cache_write_5m": write_5m,
            "cache_write_1h": write_1h,
            "tool_name": _first_tool_name(message.get("content")),
            "is_sidechain": bool(obj.get("isSidechain")),
            "agent_id": obj.get("agentId") or "",
            **_attribution(obj),
            # Not a turn column — add_turn ignores it. Carried here because it is
            # the exact project path, which the directory name only approximates.
            "cwd": obj.get("cwd") or "",
        }
        if not turn["session_id"] or turn["ts"] is None:
            continue
        if not any((turn["input_tokens"], turn["output_tokens"],
                    turn["cache_read"], turn["cache_creation"])):
            continue                      # a turn that used nothing is not a turn

        if turn["message_id"]:
            by_id[turn["message_id"]] = turn
        else:
            anonymous.append(turn)

    return list(by_id.values()) + anonymous


def parse_compactions(lines) -> list[dict]:
    """Extract context compactions from transcript lines.

    Claude Code writes one `system` / `compact_boundary` record per compaction,
    and it is the only place a compaction is stated as a fact: from the metric
    side all you get is a sudden fall in context fill, which a per-minute sampler
    can miss entirely and which cannot tell an auto-compaction from a `/compact`.

    `preTokens` is how full the window was when it gave — the number that makes
    "this project keeps hitting the ceiling" measurable rather than anecdotal."""
    out: list[dict] = []
    for obj in iter_records(lines, _COMPACT_MARKER, "compact_boundary",
                            type_field="subtype"):
        ts = _parse_ts(obj.get("timestamp"))
        session_id = obj.get("sessionId") or ""
        if not session_id or ts is None:
            continue
        meta = obj.get("compactMetadata")
        meta = meta if isinstance(meta, dict) else {}
        out.append({
            "session_id": session_id,
            "ts": ts,
            "uuid": str(obj.get("uuid") or ""),
            "trigger": str(meta.get("trigger") or ""),
            "pre_tokens": _int(meta.get("preTokens")) or None,
            "cwd": obj.get("cwd") or "",
        })
    return out


def _scan_file(store: HistoryStore, path: Path,
               positions: Optional[dict] = None) -> tuple[int, int, bool, str, set]:
    """Ingest one transcript.

    Returns (turns added, compactions added, file was read, cwd seen, session ids
    contributed to). The session ids are read out of the records rather than taken
    from the filename: a subagent transcript is named `agent-<id>.jsonl` and
    belongs to the session in its directory, so a caller inferring the id from the
    path would file every subagent under a session that does not exist.

    ``positions`` is the whole scan_state table, read once by :func:`scan`; pass
    None to look this path up on its own."""
    empty: tuple[int, int, bool, str, set] = (0, 0, False, "", set())
    try:
        stat = path.stat()
    except OSError:
        return empty

    key = str(path)
    if positions is None:
        seen_mtime, seen_lines = store.scan_position(key)
    else:
        seen_mtime, seen_lines = positions.get(key, (0.0, 0))
    if stat.st_mtime == seen_mtime:
        return empty

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            # Skip the lines already ingested instead of materialising them: an
            # active transcript runs to tens of MB and only its tail is new, so
            # reading the whole file into a list allocated the entire history to
            # throw all but the last few lines of it away.
            skipped = 0
            while skipped < seen_lines and handle.readline():
                skipped += 1
            if skipped < seen_lines:
                # The file shrank, so it is not the append-only file we recorded —
                # a rotation or a rewrite. Start over rather than reading from an
                # offset that now points into the middle of a different document.
                logger.info("Transcript %s shrank; rescanning from the start",
                            path.name)
                handle.seek(0)
                skipped = 0
            tail = handle.readlines()
    except OSError:
        logger.debug("could not read %s", path, exc_info=True)
        return empty

    if tail and not tail[-1].endswith("\n"):
        # A transcript caught mid-append: the writer has not finished this line.
        # Counting it would make the next scan skip the now-complete line and
        # lose that turn's tokens/cost from history.db permanently, so the
        # unterminated tail is neither parsed nor consumed — the stored
        # position stays just before it.
        tail.pop()

    total_lines = skipped + len(tail)

    added = 0
    cwd = ""
    sessions: set = set()
    for turn in parse_turns(tail):
        cwd = turn.get("cwd") or cwd
        fields = {k: v for k, v in turn.items() if k not in ("session_id", "ts")}
        fields.setdefault("provider", "claude")
        if store.add_turn(turn["session_id"], turn["ts"], **fields):
            added += 1
            sessions.add(turn["session_id"])

    compacted = 0
    for event in parse_compactions(tail):
        cwd = cwd or event.get("cwd") or ""
        if store.add_compaction(event["session_id"], event["ts"],
                                uuid=event["uuid"], trigger=event["trigger"],
                                pre_tokens=event["pre_tokens"]):
            compacted += 1
            sessions.add(event["session_id"])

    store.set_scan_position(key, stat.st_mtime, total_lines)
    return added, compacted, True, cwd, sessions


def _reprice_session(store: HistoryStore, session_id: str) -> Optional[float]:
    """Sum this session's cost per (model, day), then write it as an estimate.

    Per-model because a session that switches models cannot be priced at one
    rate; per-day because promotional rates expire on a date. Returns None when
    no turn could be priced at all."""
    priced = 0.0
    any_priced = False
    for group in store.session_token_totals(session_id):
        cost = pricing.cost_usd(
            group.get("model"),
            input_tokens=group.get("input_tokens") or 0,
            output_tokens=group.get("output_tokens") or 0,
            cache_read=group.get("cache_read") or 0,
            cache_write_5m=group.get("cache_creation") or 0,
            day=group.get("day"),
        )
        if cost is not None:
            priced += cost
            any_priced = True
    if not any_priced:
        return None
    store.set_estimated_cost(session_id, priced)
    return priced


def _project_dir(path: Path) -> str:
    """The `-Users-x-Documents-repo` directory a transcript belongs to.

    A session transcript sits directly in it; a subagent transcript sits two
    levels down, in `<session-id>/subagents/`."""
    if path.parent.name == "subagents":
        return path.parents[2].name
    return path.parent.name


def _transcript_paths(projects_dir: Path) -> list[Path]:
    """Every transcript, session-level and subagent-level.

    Two globs rather than `**/*.jsonl`: the recursive form would also sweep up
    whatever else ends in `.jsonl` under a project directory, and these two are
    the shapes Claude Code actually writes."""
    return sorted(projects_dir.glob("*/*.jsonl")) + \
        sorted(projects_dir.glob("*/*/subagents/*.jsonl"))


def _generated_titles() -> dict[str, str]:
    """Titles Dark Army already minted (`titles.json`). Hits only — a miss is
    remembered in memory and never written, so an empty value here means we
    have no name, not that we tried and failed."""
    try:
        data = json.loads(TITLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        sid: title.strip()
        for sid, title in data.items()
        if isinstance(sid, str) and isinstance(title, str) and title.strip()
    }


# Transcripts that yielded no title, path -> mtime at the miss. This pass runs
# every ~15 minutes for the life of the daemon, and `_first_user_prompt` is an
# uncached full-file stream — so a permanently untitled transcript was re-read
# whole on every pass, forever. A miss is remembered until the file changes;
# an unchanged file cannot answer differently than it did last time.
_title_misses: dict[str, float] = {}


def _apply_missing_titles(store: HistoryStore, paths: list[Path]) -> int:
    """Fill History rows that still have no name.

    Incremental turn-scan skips an unchanged file on one stat, so a session
    ingested before titles were written would stay nameless forever without
    this pass. Only *empty* titles are touched — see
    :meth:`HistoryStore.fill_title`."""
    from .ai_title import history_title    # deferred: ai_title imports us

    untitled = store.untitled_session_ids()
    if not untitled:
        return 0
    by_id = {path.stem: path for path in paths if path.parent.name != "subagents"}
    generated = _generated_titles()
    filled = 0
    for sid in untitled:
        path = by_id.get(sid)
        gen = generated.get(sid, "")
        mtime = 0.0
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                path = None
        # A generated title short-circuits the stream inside history_title
        # (read_ai_title is mtime-cached), so only the memoised miss on an
        # unchanged transcript is worth skipping.
        if (path is not None and not gen
                and _title_misses.get(str(path)) == mtime):
            continue
        title = history_title(str(path) if path is not None else "", sid, gen)
        if title and store.fill_title(sid, title):
            filled += 1
            if path is not None:
                _title_misses.pop(str(path), None)
        elif path is not None:
            _title_misses[str(path)] = mtime
    return filled


def scan(store: HistoryStore, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> dict:
    """Scan every transcript once. Blocking — call it from an executor.

    Safe to run repeatedly: unchanged files cost one stat, and turns already
    recorded are ignored rather than duplicated."""
    summary = {"files_seen": 0, "files_read": 0, "turns_added": 0,
               "compactions_added": 0, "sessions_priced": 0,
               "titles_filled": 0}
    try:
        paths = _transcript_paths(projects_dir)
    except OSError:
        logger.warning("Could not list %s", projects_dir, exc_info=True)
        return summary

    # One read of scan_state for the whole sweep. Nearly every file is unchanged,
    # and the per-file lookup this replaces was a separate query each time.
    try:
        positions = store.scan_positions()
    except Exception:
        logger.debug("Could not bulk-read scan positions", exc_info=True)
        positions = None

    touched: dict[str, tuple[str, str]] = {}
    for path in paths:
        summary["files_seen"] += 1
        added, compacted, was_read, cwd, sessions = _scan_file(store, path, positions)
        if was_read:
            summary["files_read"] += 1
        summary["compactions_added"] += compacted
        # A compaction on its own is enough to touch the session: it needs a
        # session row for its project to be known, and a transcript can contribute
        # one without contributing a turn the tail had not already recorded.
        if added or compacted:
            summary["turns_added"] += added
            for session_id in sessions:
                # A session's own transcript and its subagents' both land here.
                # Whichever ran first wins the cwd only if the later one has none —
                # they are the same session, so they agree.
                seen_dir, seen_cwd = touched.get(session_id, ("", ""))
                touched[session_id] = (seen_dir or _project_dir(path),
                                       seen_cwd or cwd)

    for session_id, (project_dir, cwd) in touched.items():
        # Sessions the live writer never saw have no row yet — creating them is
        # the point of the backfill. COALESCE in upsert_session means this cannot
        # blank a project or title the live writer already established.
        first, last = store.session_time_range(session_id)
        store.upsert_session(
            session_id,
            project=(os.path.basename(cwd.rstrip("/")) if cwd
                     else _project_from_dir(project_dir)),
            cwd=cwd or None,
            first_seen=first, last_seen=last,
            provider="claude",
        )
        if _reprice_session(store, session_id) is not None:
            summary["sessions_priced"] += 1

    # Titles are not in the turn records the incremental scan reads, and an
    # unchanged file is never opened again — so this is its own pass, over
    # whatever is still blank, including sessions ingested before a title
    # existed. Cheap after the first fill: the remaining untitled rows have
    # no transcript and no generated name, and we do not invent one.
    summary["titles_filled"] = _apply_missing_titles(store, paths)

    if summary["turns_added"] or summary["compactions_added"] or summary["titles_filled"]:
        logger.info("Transcript scan: %s", summary)
    return summary
