"""`sessions.json`: the per-session state the daemon keeps across a restart.

One envelope, `{"sessions": {...}, "pending_questions": {...}}`, written
atomically at 0600. Some keys are only true inside the process that wrote
them (a pid the OS may have handed to someone else since, a monotonic clock
reading), so they are left out on the way out or dropped on the way in; a
session's `subagents` is a set in memory and a sorted list on disk. An older
file with the sessions at the top level, or one carrying keys no current
build writes, still loads. An older build reading this file finds nothing it
does not already know how to read past.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .paths import SESSIONS_PATH, STATE_DIR, atomic_write_json, ensure_state_dir

logger = logging.getLogger("dark-army")

#: Keys that mean nothing once the writing process is gone. The first is
#: never written; all three are dropped from an older file on read.
_PROCESS_ONLY = ("last_event_monotonic",)
_DROPPED_ON_READ = ("pid", "last_event_monotonic", "async_park_at")

_UNREADABLE = (FileNotFoundError, json.JSONDecodeError, OSError, ValueError)


def _make_room(path: Path) -> None:
    # The state folder has its own permissions discipline; anywhere else
    # (a test's temp dir) just needs to exist.
    if path.parent == STATE_DIR:
        ensure_state_dir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)


def _on_disk(state: dict) -> dict:
    kept = {key: value for key, value in state.items() if key not in _PROCESS_ONLY}
    if "subagents" in kept:
        kept["subagents"] = sorted(kept["subagents"])
    return kept


def save_sessions(sessions: dict[str, dict], path: Path = SESSIONS_PATH,
                  pending_questions: dict[str, dict] | None = None) -> None:
    """Write every session, and the questions sessions are stopped on.

    The questions sit beside the sessions rather than inside them: they are
    not state-machine fields, `load_pending_questions` filters them against
    what survived the restart, and a downgraded build rewriting a session
    must never carry one forward as if it were its own. A failed write is a
    log line; the next structural change writes again.
    """
    envelope: dict = {"sessions": {sid: _on_disk(state) for sid, state in sessions.items()}}
    if pending_questions:
        envelope["pending_questions"] = {
            sid: question for sid, question in pending_questions.items()
            if isinstance(question, dict) and question
        }
    _make_room(path)
    try:
        # The helper fsyncs before the rename, so a crash cannot leave a
        # half-written file under the real name.
        atomic_write_json(path, envelope, mode=0o600)
    except OSError:
        logger.warning("Could not write session state to %s", path)


def _restored(state: object) -> Optional[dict]:
    """One entry made safe to resume, or None when it is not a session."""
    if not isinstance(state, dict) or "state" not in state:
        return None
    if not isinstance(state.get("last_event"), (int, float)):
        return None
    for key in _DROPPED_ON_READ:
        state.pop(key, None)
    # The async-child map is read by iterating it; a string from a damaged
    # file would be walked a character at a time, one phantom child per
    # letter. Both container shapes are readable, anything else goes.
    if "subagents_async" in state and not isinstance(state["subagents_async"], (dict, list)):
        del state["subagents_async"]
    if "subagent_spawns" in state and not isinstance(state["subagent_spawns"], list):
        del state["subagent_spawns"]
    live = state.pop("subagents", None)
    if isinstance(live, list):
        state["subagents"] = set(live)
    return state


def load_sessions(path: Path = SESSIONS_PATH) -> dict[str, dict]:
    """Every restorable session in the file, or `{}` when it cannot be read.

    Accepts the envelope and the older top-level layout alike; keys an older
    daemon wrote and no current reader wants (`session_order`,
    `next_display_id`) are simply not looked at.
    """
    if path.parent == STATE_DIR:
        ensure_state_dir()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except _UNREADABLE:
        return {}
    if not isinstance(document, dict):
        return {}
    wrapped = document.get("sessions")
    entries = wrapped if isinstance(wrapped, dict) else document
    resumed = {}
    for sid, state in entries.items():
        state = _restored(state)
        if state is not None:
            resumed[sid] = state
    return resumed


def load_pending_questions(path: Path = SESSIONS_PATH) -> dict[str, dict]:
    """The questions sessions were stopped on when the daemon last wrote.

    Separate from `load_sessions` — and read out of the same file — because the
    caller has to filter these against the sessions that survived its own
    startup prune, and against the state each one was restored in. Merging them
    into the state dict would hand the state machine a key it has no opinion
    about.

    Why persist at all: an `AskUserQuestion` exists in the transcript only once
    it has been *answered* (see `session_stats.question_from_tool_input`), so
    the `PreToolUse` hook is the sole source of the text while the dialog is
    actually up. Held in memory alone, a menu-bar restart — which the rebuild
    row in the panel invites — erased the one thing the row had to say, and the
    session went on blocking for as long as the human took to notice. Measured:
    a question raised at 00:23:19 and still standing at 00:31, drawn as the
    prose of the turn before it, because the app restarted at 00:26:40.

    Returns `{}` on anything unreadable; a missing or malformed key is a panel
    one line poorer, never a daemon that will not start.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("pending_questions")
    if not isinstance(raw, dict):
        return {}
    return {
        sid: q for sid, q in raw.items()
        if isinstance(sid, str) and sid and isinstance(q, dict) and q
    }
