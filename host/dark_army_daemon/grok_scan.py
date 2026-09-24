"""Backfill: Grok ``updates.jsonl`` → the history database.

The live writer only records a Grok session Dark Army is watching right now, and
only as a session-level total. This scanner walks ``~/.grok/sessions/**/updates.jsonl``
for every session that has ever run on this machine — including ones from
before Dark Army was installed — and writes one measured turn per usage record.

Same incremental contract as ``transcript_scan``: skip an unchanged file on
one stat, read only the appended tail of a grown one, INSERT OR IGNORE on
``message_id`` so a re-read contributes once.

Cost is Grok's stamped ticks, never a price table. A session that has even
one unpriced turn gets no session-level cost — absence means unknown, and a
partial sum wearing a dollar sign is the failure mode Grok's own docs call
out. ``set_estimated_cost`` is not used here; it exists to protect a
measured Claude figure from an estimate, and Grok has no estimate path.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from . import grok_roster
from .grok_usage import parse_turn_record, read_jsonl_delta
from .history import COST_MEASURED, HistoryStore

logger = logging.getLogger("dark-army.grok-scan")


def _updates_paths(sessions_dir: Path) -> list[Path]:
    """Every ``updates.jsonl`` one level under an encoded cwd.

    ``~/.grok/sessions/<urlencoded-cwd>/<session-id>/updates.jsonl`` — two
    segments, not ``**``, so a stray jsonl deeper in the tree is not ingested.
    """
    return sorted(sessions_dir.glob("*/*/updates.jsonl"))


def parse_turns(lines, session_id: str = "") -> list[dict]:
    """Extract usage turns from update lines. ``session_id`` fills in when
    the record itself omitted ``params.sessionId`` (the directory name is
    authoritative in that case)."""
    out: list[dict] = []
    for line in lines:
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        turn = parse_turn_record(obj)
        if turn is None:
            continue
        if not turn["session_id"]:
            turn["session_id"] = session_id
            if session_id and turn["ts"] is not None:
                turn["message_id"] = f"grok:{session_id}:{int(turn['ts'])}"
        if not turn["session_id"] or not turn["message_id"]:
            continue
        out.append(turn)
    return out


def _scan_file(store: HistoryStore, path: Path, session_id: str,
               positions: Optional[dict] = None) -> tuple[int, bool]:
    """Ingest one ``updates.jsonl``. Returns (turns added, file was read).

    The position in ``scan_state`` is a **byte offset, stored negated** in the
    ``lines`` column: the old line count meant re-walking the whole file to
    skip lines already read, on every mtime change — and ``updates.jsonl``'s
    mtime moves on every turn. Negation is what keeps the shared column
    forward- and backward-compatible: a legacy non-negative line count is
    simply rescanned from byte 0 once (INSERT OR IGNORE on ``message_id``
    makes that free of duplicates), and an older build reading a negative
    value skips nothing and rescans, which is equally safe.

    ``read_jsonl_delta`` holds a half-written trailing line out of the offset
    (the ``tail_is_complete`` rule), so a torn record is read whole on the
    next pass rather than lost behind a position that already counted it.
    """
    try:
        stat = path.stat()
    except OSError:
        return 0, False

    key = str(path)
    if positions is None:
        seen_mtime, seen = store.scan_position(key)
    else:
        seen_mtime, seen = positions.get(key, (0.0, 0))
    if stat.st_mtime == seen_mtime:
        return 0, False

    offset = -int(seen) if seen < 0 else 0     # legacy line counts rescan once
    if offset > stat.st_size:
        # Shrunk or replaced: not the append-only file the offset was about.
        logger.info("Grok updates %s shrank; rescanning from the start",
                    path.parent.name)
        offset = 0

    tail: list[str] = []
    try:
        offset = read_jsonl_delta(path, offset, tail.append)
    except OSError:
        logger.debug("could not read %s", path, exc_info=True)
        return 0, False

    added = 0
    for turn in parse_turns(tail, session_id):
        fields = {k: v for k, v in turn.items() if k not in ("session_id", "ts")}
        if store.add_turn(turn["session_id"], turn["ts"], **fields):
            added += 1

    store.set_scan_position(key, stat.st_mtime, -offset)
    return added, True


def _read_summary(directory: Path) -> dict:
    try:
        raw = (directory / "summary.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_prompt(directory: Path) -> str:
    """The instruction the person opened with, when Grok never minted a title."""
    return grok_roster.first_prompt_from_dir(directory)


def _apply_missing_titles(store: HistoryStore, directories: dict) -> int:
    """Fill Grok History rows that still have no name.

    Same reason as the Claude pass: an unchanged ``updates.jsonl`` is never
    re-opened, so a session ingested before Grok wrote ``generated_title``
    would stay blank. Official titles still go through ``upsert_session``
    (they overwrite); this only fills empties, from the summary or the
    opening prompt."""
    untitled = set(store.untitled_session_ids())
    if not untitled:
        return 0
    filled = 0
    for session_id, directory in directories.items():
        if session_id not in untitled:
            continue
        summary_data = _read_summary(directory)
        title = grok_roster.title_of(summary_data) or _first_prompt(directory)
        if title and store.fill_title(session_id, title):
            filled += 1
    return filled


def _session_cost(store: HistoryStore, session_id: str) -> Optional[float]:
    """Sum measured turn costs. None if any turn is unpriced or none exist."""
    rows = store._query(
        "SELECT COUNT(*) AS turns,"
        " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced,"
        " SUM(cost_usd) AS cost"
        " FROM turns WHERE session_id = ?",
        (session_id,),
    )
    if not rows:
        return None
    turns = rows[0]["turns"] or 0
    priced = rows[0]["priced"] or 0
    if not turns or priced < turns:
        return None
    return float(rows[0]["cost"] or 0.0)


def scan(store: HistoryStore,
         sessions_dir: Optional[Path] = None) -> dict:
    """Scan every Grok ``updates.jsonl`` once. Blocking — call from an executor.

    Safe to run repeatedly: unchanged files cost one stat, and turns already
    recorded are ignored rather than duplicated."""
    if sessions_dir is None:
        sessions_dir = grok_roster.SESSIONS_DIR
    summary = {"files_seen": 0, "files_read": 0, "turns_added": 0,
               "sessions_priced": 0, "titles_filled": 0}
    try:
        paths = _updates_paths(sessions_dir)
    except OSError:
        logger.warning("Could not list %s", sessions_dir, exc_info=True)
        return summary

    try:
        positions = store.scan_positions()
    except Exception:
        logger.debug("Could not bulk-read scan positions", exc_info=True)
        positions = None

    touched: dict[str, Path] = {}
    directories: dict[str, Path] = {}
    for path in paths:
        summary["files_seen"] += 1
        session_id = path.parent.name
        directories[session_id] = path.parent
        added, was_read = _scan_file(store, path, session_id, positions)
        if was_read:
            summary["files_read"] += 1
        if added:
            summary["turns_added"] += added
            touched[session_id] = path.parent

    for session_id, directory in touched.items():
        # Recover cwd from summary.json rather than unquoting the encoded
        # folder name — the encoding is lossy around some characters.
        summary_data = _read_summary(directory)
        cwd = grok_roster.cwd_of(summary_data)
        title = grok_roster.title_of(summary_data)
        model = ""
        if isinstance(summary_data.get("current_model_id"), str):
            model = summary_data["current_model_id"]
        first, last = store.session_time_range(session_id)
        fields = {
            "project": (os.path.basename(cwd.rstrip("/")) if cwd else None),
            "cwd": cwd or None,
            "title": title or None,
            "primary_model": model or None,
            "first_seen": first,
            "last_seen": last,
            "provider": "grok",
        }
        cost = _session_cost(store, session_id)
        if cost is not None:
            fields["cost_usd"] = cost
            fields["cost_source"] = COST_MEASURED
            summary["sessions_priced"] += 1
        store.upsert_session(session_id, **{k: v for k, v in fields.items()
                                            if v is not None})

    summary["titles_filled"] = _apply_missing_titles(store, directories)

    if summary["turns_added"] or summary["titles_filled"]:
        logger.info("Grok history scan: %s", summary)
    return summary
