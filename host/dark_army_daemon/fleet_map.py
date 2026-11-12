"""The fleet activity map's rules: which station a tool call belongs to, when a
call is over, and the section the panel draws.

Pure and stdlib-only (one import from `protocol`, the module both packages
already share). The daemon owns one `CallRing` per session, loop-only, memory
only; `compose` turns the rings and the published agent rows into the
`fleet_activity` section of `/api/state`. That section is loopback-only
(`api_server.phone_picture` strips it) and carries tool and station names and
a sequence number, never a timestamp, a path, a command or a key: a time field
under any name outside `_CLOCK_FIELDS` would make every frame news for ever.

There is no general "call ended" hook, so an end is inferred: a call is
`running` from its `tool_use` until that session's next event. Full contract:
`docs/fleet-map-contract.md`.
"""

from collections import deque

from .protocol import is_ask_user_question

# Ordered: the panel draws the ring in this order.
STATIONS = (
    ("you", "You"),
    ("files", "Files"),
    ("shell", "Shell"),
    ("web", "Web"),
    ("helpers", "Helpers"),
    ("skills", "Skills"),
    ("tools", "Tools"),
)
STATION_IDS = frozenset(sid for sid, _ in STATIONS)

MAX_CALLS_PER_SESSION = 8
_TOOL_NAME_LIMIT = 64

_FILES = frozenset({
    "Read", "read_file", "Grep", "grep", "Glob", "list_dir",
    "Edit", "Write", "write", "search_replace", "NotebookEdit",
    "NotebookRead", "MultiEdit",
})
_SHELL = frozenset({
    "Bash", "run_terminal_command", "BashOutput", "KillShell", "KillBash",
})
_WEB = frozenset({"WebSearch", "web_search", "WebFetch", "web_fetch"})
_HELPERS = frozenset({"Agent", "Task", "spawn_subagent"})
_SKILLS = frozenset({"Skill", "SlashCommand"})


def station_for(tool_name) -> str:
    """The one place a tool name becomes a station. The panel never re-derives
    it. Ask tools wait on the person (`you`); MCP tools (`mcp__*`,
    `server__tool`), `LSP` and anything unknown are `tools`."""
    name = tool_name if isinstance(tool_name, str) else ""
    if is_ask_user_question(name):
        return "you"
    if name in _FILES:
        return "files"
    if name in _SHELL:
        return "shell"
    if name in _WEB:
        return "web"
    if name in _HELPERS:
        return "helpers"
    if name in _SKILLS:
        return "skills"
    return "tools"


class CallRing(deque):
    """One session's last calls, oldest first, plus its failed-call count."""

    def __init__(self):
        super().__init__(maxlen=MAX_CALLS_PER_SESSION)
        self.errors = 0
        # Failed calls per station, outliving the eight-call window: kept until
        # the agent's next prompt zeroes it with `errors`.
        self.errors_by_station: dict = {}


def _close(ring, end: str) -> bool:
    """Close the open entry (there is at most one, the newest)."""
    if ring and ring[-1]["end"] == "running":
        ring[-1]["end"] = end
        return True
    return False


def _append(ring, station: str, tool: str, seq: int) -> None:
    ring.append({"seq": seq, "station": station,
                 "tool": tool[:_TOOL_NAME_LIMIT], "end": "running"})


def advance(ring, event: str, hook: str, tool_name: str, seq: int) -> bool:
    """Step one session's ring by one hook event; True when it changed.

    `tool_use` closes the open call as done and opens a new one;
    `permission` parks a running call on `you` unless one already is there;
    `tool_done`, `add` and `dismiss` close as done; `tool_failed` closes as
    failed and steps `errors`; a `dismiss` from `UserPromptSubmit` zeroes
    `errors`. Anything else (`session_start`, `compact`, …) is a no-op."""
    tool = tool_name if isinstance(tool_name, str) else ""
    if event == "tool_use":
        _close(ring, "done")
        _append(ring, station_for(tool), tool, seq)
        return True
    if event == "permission":
        if ring and ring[-1]["end"] == "running" and ring[-1]["station"] == "you":
            return False
        _close(ring, "done")
        _append(ring, "you", tool or "permission", seq)
        return True
    if event == "tool_failed":
        if ring and ring[-1]["end"] == "running":
            station = ring[-1]["station"]
            ring.errors_by_station[station] = ring.errors_by_station.get(station, 0) + 1
        _close(ring, "failed")
        ring.errors += 1
        return True
    if event in ("tool_done", "add", "dismiss"):
        changed = _close(ring, "done")
        if event == "dismiss" and hook == "UserPromptSubmit" and ring.errors:
            ring.errors = 0
            ring.errors_by_station = {}
            changed = True
        return changed
    return False


def station_counts(tool_counts) -> dict:
    """A session's lifetime tool counts folded onto stations."""
    out: dict = {}
    if not isinstance(tool_counts, dict):
        return out
    for tool, n in tool_counts.items():
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        station = station_for(tool)
        out[station] = out.get(station, 0) + n
    return out


def _rows_by_session(rows) -> dict:
    """Flatten the published agents snapshot (`{"waiting": [...], ...}`) or a
    plain list of rows into `{session_id: row}`."""
    flat = []
    if isinstance(rows, dict):
        for group in rows.values():
            if isinstance(group, list):
                flat.extend(group)
    elif isinstance(rows, list):
        flat = rows
    out = {}
    for row in flat:
        if isinstance(row, dict):
            sid = str(row.get("session_id") or "")
            if sid:
                out[sid] = row
    return out


def compose(rings, rows) -> dict:
    """The `fleet_activity` section. No timestamps, no paths, no keys."""
    by_sid = _rows_by_session(rows)
    sessions = {}
    for sid, ring in rings.items():
        row = by_sid.get(sid) or {}
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        sessions[sid] = {
            "calls": [dict(c) for c in ring],
            "station_counts": station_counts(stats.get("tool_counts")),
            "errors": int(getattr(ring, "errors", 0) or 0),
            "errors_by_station": dict(getattr(ring, "errors_by_station", {}) or {}),
        }
    return {
        "version": 1,
        "available": True,
        "stations": [{"id": i, "label": label} for i, label in STATIONS],
        "sessions": sessions,
    }
