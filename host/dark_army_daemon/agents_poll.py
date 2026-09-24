"""Ground truth about live sessions, straight from Claude Code.

`claude agents --json` is the supported way to enumerate sessions, and it knows
things the hook stream cannot:

  * **background agents**, which never emit the terminal-shaped events the rest of
    the daemon is built around, and so were previously invisible — including the
    ``blocked`` ones, i.e. exactly the sessions a human has forgotten about;
  * the session's real ``name`` (what ``--name`` / ``/rename`` set), where the
    daemon otherwise guesses a title out of the transcript (see ai_title.py);
  * whether a session is alive at all, without inferring it from PID liveness.

This module is deliberately read-only and failure-tolerant: it returns None on any
problem rather than an empty list, so a caller can tell "Claude says there are no
sessions" apart from "we could not ask". Nothing here evicts or mutates session
state — reconciliation is additive enrichment.
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import subprocess_env

logger = logging.getLogger("dark-army.agents")

# A .app launched from Finder inherits a minimal PATH (/usr/bin:/bin:/usr/sbin:
# /sbin), and `claude` is on none of it — verified on this machine, where it lives
# in Homebrew. Without these fallbacks the poller would simply never find the
# binary inside the bundle, and would do it silently.
CLAUDE_CANDIDATES = (
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.local/bin/claude",
    "~/.bun/bin/claude",
    "~/.npm-global/bin/claude",
)

POLL_INTERVAL_SECONDS = 10.0
QUERY_TIMEOUT_SECONDS = 8.0

# Polling `claude agents --json` means forking the Claude Code CLI — a Bun
# runtime — and at one spawn per 10s that is 8,640 launches a day costing about
# 240ms of CPU each: ~2.4% of a core, burnt continuously, to re-read a listing
# that changes a handful of times an hour.
#
# So the interval backs off once the listing stops moving, up to
# POLL_MAX_INTERVAL_SECONDS. Responsiveness does not depend on the interval: six
# call sites already `poll_soon()` on the events that matter (session start and
# end, state changes, the panel opening), and any of them resets the backoff. The
# interval only bounds how late we notice a change *nothing told us about* — a
# background agent finishing on its own, or a `claude` session started elsewhere.
POLL_MAX_INTERVAL_SECONDS = 60.0
POLL_BACKOFF_AFTER = 3          # unchanged polls before slowing down

# How long a background agent may sit `blocked` before it stops being an alert and
# becomes ambient. `claude agents --json` lists abandoned sessions indefinitely —
# on this machine two have been blocked for 13 and 20 days, with no transcript
# write since. Treating those as "needs you" would pin a warning to the menu bar
# forever, which teaches the eye to ignore the warning that matters.
STALE_BLOCKED_SECONDS = 12 * 3600.0

# Background sessions report `state`, interactive ones report `status`. Both are
# free-form as far as we are concerned: we normalise the two we act on and pass
# anything else through rather than dropping a session we don't recognise.
BLOCKED = "blocked"
BUSY = "busy"
IDLE = "idle"


@dataclass
class AgentRecord:
    """One row of `claude agents --json`, normalised."""

    session_id: str
    name: str = ""
    kind: str = ""              # "interactive" | "background"
    activity: str = ""          # normalised `status`/`state`
    pid: Optional[int] = None
    cwd: str = ""
    started_at: Optional[float] = None   # epoch seconds
    short_id: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_background(self) -> bool:
        return self.kind == "background"

    @property
    def needs_attention(self) -> bool:
        """A background agent parked on `blocked` is waiting for a human and will
        wait forever — it has no terminal to catch anyone's eye."""
        return self.activity == BLOCKED

    @property
    def project(self) -> str:
        return Path(self.cwd).name if self.cwd else ""


def find_claude_binary() -> Optional[str]:
    """Absolute path to the `claude` CLI, or None. Resolved fresh each call: the
    Homebrew cask path carries a version number, so a Claude Code upgrade moves the
    target — only the symlink is stable, and a cached miss would outlive the fix."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def parse_agents_json(payload: str) -> Optional[list[AgentRecord]]:
    """Parse the CLI's JSON array. None if it isn't the shape we expect — a future
    CLI that changes the contract should make us go quiet, not make us invent
    sessions."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.debug("agents --json returned non-JSON")
        return None
    if not isinstance(data, list):
        logger.debug("agents --json returned %s, expected a list", type(data).__name__)
        return None

    records = []
    for item in data:
        if not isinstance(item, dict):
            continue
        session_id = item.get("sessionId") or ""
        if not session_id:
            continue  # nothing we could correlate it with
        started = item.get("startedAt")
        records.append(AgentRecord(
            session_id=session_id,
            name=item.get("name") or "",
            kind=item.get("kind") or "",
            # Interactive rows carry `status`, background rows carry `state`.
            activity=(item.get("status") or item.get("state") or "").lower(),
            pid=item.get("pid") if isinstance(item.get("pid"), int) else None,
            cwd=item.get("cwd") or "",
            started_at=started / 1000.0 if isinstance(started, (int, float)) else None,
            short_id=item.get("id") or session_id[:8],
            raw=item,
        ))
    return records


async def query_agents(
    timeout: float = QUERY_TIMEOUT_SECONDS,
    on_error: Optional[Callable[[str], None]] = None,
) -> Optional[list[AgentRecord]]:
    """Run `claude agents --json` once. None on any failure at all.

    `on_error` receives a short reason for the failure. Every path here used to be
    a debug log, which made an unavailable reconciler completely undiagnosable in
    a built app: background agents simply never appeared and nothing said why."""
    def fail(reason: str) -> None:
        if on_error is not None:
            on_error(reason)

    binary = find_claude_binary()
    if not binary:
        fail("the `claude` CLI was not found on PATH or in the usual locations")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "agents", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Never inherit the parent's working directory. The menu bar app runs
            # from inside its own bundle, and `build.sh` does `rm -rf build dist`
            # before recreating it — so after a rebuild-and-relaunch the process
            # holds a cwd pointing at a deleted inode, and every child it spawns
            # dies on it. Claude Code is a Bun binary and reports that as a bare
            # "ENOENT: Bun could not find a file", which names neither the file
            # nor the cwd. Home always exists, and `agents --json` is global —
            # it lists every session regardless of where it runs.
            cwd=str(Path.home()),
            env=subprocess_env.clean_env(),
        )
    except OSError as exc:
        fail(f"could not run {binary}: {exc}")
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()          # reap it; a zombie every 10s adds up
        except (ProcessLookupError, OSError):
            pass
        fail(f"`claude agents --json` timed out after {timeout:.0f}s")
        return None

    if proc.returncode != 0:
        detail = (stderr or b"")[:200].decode("utf-8", "replace").strip()
        fail(f"`claude agents --json` exited {proc.returncode}: {detail or 'no output'}")
        return None

    records = parse_agents_json((stdout or b"").decode("utf-8", "replace"))
    if records is None:
        fail("`claude agents --json` returned something this build cannot parse")
    return records


class AgentsPoller:
    """Polls `claude agents --json` on an interval and hands the result to a
    callback. Owns no state beyond the task itself."""

    def __init__(
        self,
        on_records: Callable[[list[AgentRecord]], None],
        interval: float = POLL_INTERVAL_SECONDS,
    ):
        self._on_records = on_records
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self.available: Optional[bool] = None   # None until the first attempt
        self.last_error: str = ""

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def poll_soon(self) -> None:
        """Ask for a refresh before the next tick — e.g. the panel just opened."""
        self._wake.set()

    async def poll_once(self) -> Optional[list[AgentRecord]]:
        records = await query_agents(on_error=self._record_error)
        self.available = records is not None
        if records is not None:
            if self.last_error:
                logger.info("`claude agents --json` is working again")
                self.last_error = ""
            try:
                self._on_records(records)
            except Exception:
                logger.warning("agents poll callback failed", exc_info=True)
        return records

    def _record_error(self, reason: str) -> None:
        """Log a poll failure once per distinct reason. Once per 10s forever would
        be noise, but staying silent is how this went undiagnosable in the first
        place — the reason is also surfaced through the API."""
        if reason != self.last_error:
            logger.warning("Background agents unavailable: %s", reason)
        self.last_error = reason

    @staticmethod
    def _fingerprint(records: Optional[list]) -> Optional[tuple]:
        """What counts as "the listing moved". Deliberately not the whole record:
        `started_at` is fixed and `raw` carries fields that jitter without meaning
        anything to us, so comparing those would defeat the backoff entirely."""
        if records is None:
            return None
        return tuple(sorted(
            (r.session_id, r.kind, r.activity, r.pid) for r in records
        ))

    async def _loop(self) -> None:
        idle_rounds = 0
        last_fp: Optional[tuple] = None
        while True:
            try:
                records = await self.poll_once()
                fingerprint = self._fingerprint(records)
                # A failed poll (None) is not "unchanged" — it is no reading at
                # all, and backing off on it would slow recovery exactly when the
                # CLI has started working again.
                if fingerprint is not None and fingerprint == last_fp:
                    idle_rounds += 1
                else:
                    idle_rounds = 0
                last_fp = fingerprint
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad poll must never end the loop — this task is the only
                # thing keeping background agents visible.
                logger.warning("agents poll failed", exc_info=True)
                idle_rounds = 0

            interval = self._interval
            if idle_rounds >= POLL_BACKOFF_AFTER:
                interval = min(
                    POLL_MAX_INTERVAL_SECONDS,
                    self._interval * 2 ** (idle_rounds - POLL_BACKOFF_AFTER + 1),
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            else:
                # Someone asked for freshness, so the listing is interesting again.
                idle_rounds = 0
            self._wake.clear()
