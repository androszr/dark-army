"""Outbound pipe to a running Grok leader.

Claude reaches a session through Dark Army's MCP channel. Grok has no
``claude/channel``. The verified analog is ACP through the leader:
``grok agent --leader stdio``, then ``session/prompt`` on a live session
id. That send does not steal the TUI.

Reachable means the leader is up **and** this session is a live client
(``resident: true``), not merely present in ``session/list`` — that list
includes dead disk history. The same honesty rule that made
``channel: true`` on every Claude row a lie.

Never starts a leader: ``grok agent --leader stdio`` with no sock is how
one gets created, so ``connect()`` refuses unless the sock is already
there. Prompt the live session; do not become its controlling client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import subprocess_env

logger = logging.getLogger("dark-army.grok-leader")

GROK_CANDIDATES = (
    "~/.grok/bin/grok",
    "/opt/homebrew/bin/grok",
    "/usr/local/bin/grok",
    "~/.local/bin/grok",
)

LEADER_SOCK = Path.home() / ".grok" / "leader.sock"

# Same order as `_send_to_channel`: return once the write is accepted, not
# once the turn ends. `session/prompt` typically does not return until then.
PROMPT_ACK_SECONDS = 2.0

# After a failed connect, do not spawn again until this elapses or the
# sock's identity changes. A broken handshake used to retry every 5s and
# registered thousands of 4ms clients on the user's leader.
LEADER_CONNECT_BACKOFF_SECONDS = 30.0

# A label the leader does not key on, proven on grok 1.0.41 by
# tools/grok_leader_probe.py on 2026-09-23.
CLIENT_NAME = "dark-army"
CLIENT_VERSION = "0.1.0"


def initialize_params() -> dict:
    """The handshake Grok's leader actually accepts.

    ``clientInfo.version`` is required (missing field → Invalid params,
    the child dies in milliseconds). ``protocolVersion`` is the integer
    1 — the string ``"1"`` is what a first draft sent.
    """
    return {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }

SESSIONS_CHANGED = "_x.ai/sessions/changed"
QUEUE_CHANGED = "_x.ai/queue/changed"
SESSION_LIST = "session/list"
SESSION_LIST_XAI = "_x.ai/session/list"


def find_grok_binary() -> Optional[str]:
    """Absolute path to the ``grok`` CLI, or None. Resolved fresh each call:
    a cached miss would outlive an install, and a Finder-launched .app gets
    a PATH that has no ``grok`` on it."""
    found = shutil.which("grok")
    if found:
        return found
    for candidate in GROK_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def leader_sock_present(path: Optional[os.PathLike[str] | str] = None) -> bool:
    """True when the leader socket exists and is a socket. OSError → False.

    ``grok agent --leader stdio`` with no sock *starts* a leader, so this
    is the whole "do not auto-start" gate.
    """
    target = Path(path) if path is not None else LEADER_SOCK
    try:
        st = target.stat()
    except OSError:
        return False
    try:
        return stat.S_ISSOCK(st.st_mode)
    except (AttributeError, TypeError):
        return target.exists()


def leader_sock_identity(path: Optional[os.PathLike[str] | str] = None):
    """``(dev, ino)`` of the leader sock, or None. A new sock is a new leader."""
    target = Path(path) if path is not None else LEADER_SOCK
    try:
        st = target.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def session_id_of(entry: dict) -> str:
    return str(entry.get("sessionId") or entry.get("session_id") or "")


def is_resident(entry: dict) -> bool:
    """A positive live-client flag. Missing or false is unreachable.

    Presence in a list is not enough — ``session/list`` includes disk history.
    """
    return entry.get("resident") is True


def resident_ids(entries) -> frozenset[str]:
    out: set[str] = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        sid = session_id_of(entry)
        if sid and is_resident(entry):
            out.add(sid)
    return frozenset(out)


def list_has_resident_flag(entries) -> bool:
    """True if any entry carries the live-client flag, even if it is false."""
    for entry in entries or ():
        if isinstance(entry, dict) and "resident" in entry:
            return True
    return False


def _session_fields(payload):
    """The dict that holds sessions/upserted/removed, or a bare list."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return {}
    params = payload.get("params")
    if isinstance(params, list):
        return params
    if isinstance(params, dict):
        return params
    return payload


def _as_entries(value) -> list:
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _as_ids(value) -> list[str]:
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, dict):
            sid = session_id_of(item)
            if sid:
                out.append(sid)
    return out


def sessions_from_payload(payload) -> list:
    """Tolerate a bare list, ``{sessions}``, ``{upserted}``, or a JSON-RPC notification."""
    body = _session_fields(payload)
    if isinstance(body, list):
        return [e for e in body if isinstance(e, dict)]
    if not isinstance(body, dict):
        return []
    for key in ("sessions", "upserted", "added"):
        entries = _as_entries(body.get(key))
        if entries:
            return entries
    if isinstance(body.get("session"), dict):
        return [body["session"]]
    return []


def _census_sessions(body) -> Optional[list]:
    """A ``sessions`` list that is a census, not a one-entry delta.

    Two or more entries is the full live set: a departed client that is
    simply omitted must leave. A one-entry list is an upsert — treating
    that as a replace is how Reply vanished on every other live row.
    """
    if isinstance(body, list):
        entries = [e for e in body if isinstance(e, dict)]
        return entries if len(entries) >= 2 else None
    if isinstance(body, dict):
        raw = body.get("sessions")
        if isinstance(raw, list):
            entries = [e for e in raw if isinstance(e, dict)]
            if len(entries) >= 2:
                return entries
    return None


def _ambiguous_census(body) -> bool:
    """A one-entry ``sessions`` list — a census and an upsert are the same bytes.

    `_census_sessions` requires two entries before it will treat a list as the
    whole live set, which is the only safe *static* reading. This names the
    case that reading gets wrong so the caller can go and ask.
    """
    if isinstance(body, list):
        return len([e for e in body if isinstance(e, dict)]) == 1
    if isinstance(body, dict):
        raw = body.get("sessions")
        if isinstance(raw, list):
            return len([e for e in raw if isinstance(e, dict)]) == 1
    return False


def merge_sessions_changed(current: frozenset[str], payload) -> frozenset[str]:
    """Apply a sessions/changed notification without treating a delta as a snapshot.

    Upserts add (``resident: true``) or drop (``resident: false``). Removals
    drop. A one-entry ``sessions`` list is merged, never a wholesale replace
    — that is how Reply vanished on every other live Grok row. A
    snapshot-shaped ``sessions`` list (two or more entries) *is* the
    resident set: a departed client omitted from it must leave.
    """
    body = _session_fields(payload)
    census = _census_sessions(body)
    if census is not None:
        out = set(resident_ids(census))
        if isinstance(body, dict):
            for key in ("upserted", "added"):
                _apply_upserts(out, _as_entries(body.get(key)))
            if isinstance(body.get("session"), dict):
                _apply_upserts(out, [body["session"]])
            for key in ("removed", "deleted"):
                for sid in _as_ids(body.get(key)):
                    out.discard(sid)
        return frozenset(out)
    out = set(current)
    if isinstance(body, list):
        entries = [e for e in body if isinstance(e, dict)]
        _apply_upserts(out, entries)
        return frozenset(out)
    if not isinstance(body, dict):
        return frozenset(current)
    for key in ("sessions", "upserted", "added"):
        _apply_upserts(out, _as_entries(body.get(key)))
    if isinstance(body.get("session"), dict):
        _apply_upserts(out, [body["session"]])
    for key in ("removed", "deleted"):
        for sid in _as_ids(body.get(key)):
            out.discard(sid)
    return frozenset(out)


def _apply_upserts(out: set[str], entries) -> None:
    for entry in entries:
        sid = session_id_of(entry)
        if not sid:
            continue
        if is_resident(entry):
            out.add(sid)
        elif "resident" in entry:
            out.discard(sid)


def _collect_strings(obj: Any, out: set[str]) -> None:
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.add(str(key))
            _collect_strings(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_strings(value, out)


def _advertises(init_result, method: str) -> bool:
    names: set[str] = set()
    _collect_strings(init_result, names)
    return method in names


class LeaderClient:
    """One ``grok agent --leader stdio`` child, owned by the daemon loop."""

    def __init__(
        self,
        on_residents: Optional[Callable[[frozenset[str]], None]] = None,
        on_down: Optional[Callable[[], None]] = None,
    ):
        self._on_residents = on_residents
        self._on_down = on_down
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._prompt_acks: dict[str, list[asyncio.Future]] = {}
        self.residents: frozenset[str] = frozenset()
        self.connected = False
        self._closing = False
        self._connecting = False
        self._honest_empty_logged = False
        self._fail_identity = None
        self._fail_at = 0.0
        self._list_methods: list[str] = []
        self._relist_task: Optional[asyncio.Task] = None

    def _schedule_relist(self) -> None:
        """Re-list residents off the reader loop, one at a time.

        Not awaited from `_handle`: the reply to the list request comes back
        *through* that same reader, so awaiting it there deadlocks until the
        timeout. One in flight at a time — a burst of ambiguous pushes should
        settle into a single authoritative answer, not a queue of them.
        """
        if not self.connected:
            return
        if self._relist_task is not None and not self._relist_task.done():
            return

        async def run() -> None:
            try:
                await self._relist_residents()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Grok resident re-list failed", exc_info=True)

        self._relist_task = asyncio.ensure_future(run())

    def _set_residents(self, ids: frozenset[str]) -> None:
        if ids == self.residents:
            return
        self.residents = ids
        if self._on_residents is not None:
            self._on_residents(ids)

    def _mark_down(self) -> None:
        was = self.connected
        self.connected = False
        self._set_residents(frozenset())
        if was and self._on_down is not None:
            self._on_down()

    def _note_connect_failure(self, identity) -> None:
        self._fail_identity = identity
        self._fail_at = time.monotonic()

    def _in_connect_backoff(self, identity) -> bool:
        if identity is None or identity != self._fail_identity:
            return False
        return (time.monotonic() - self._fail_at) < LEADER_CONNECT_BACKOFF_SECONDS

    async def connect(self) -> bool:
        if self.connected:
            return True
        if self._connecting:
            return False
        if not leader_sock_present():
            return False
        identity = leader_sock_identity()
        if self._in_connect_backoff(identity):
            return False
        binary = find_grok_binary()
        if not binary:
            logger.info("Grok binary not found; leader client idle")
            self._note_connect_failure(identity)
            return False
        self._connecting = True
        self._closing = False
        try:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    binary, "agent", "--leader", "stdio",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    # asyncio's default 64 KiB stream limit is smaller than a
                    # `session/list` reply from a busy leader; without this the
                    # reader died on ValueError and reconnected every 30s.
                    limit=8 * 1024 * 1024,
                    # Same cwd footgun `query_agents` already documents: never
                    # inherit the parent's working directory.
                    cwd=str(Path.home()),
                    # py2app's PYTHONHOME would make this Grok, and every
                    # tool it runs, boot against the frozen stdlib.
                    env=subprocess_env.clean_env(),
                )
            except OSError as exc:
                logger.info("Could not spawn grok leader client: %s", exc)
                self._proc = None
                self._note_connect_failure(identity)
                return False
            self._reader_task = asyncio.create_task(self._read_loop())
            try:
                result = await self._request(
                    "initialize", initialize_params(), timeout=5.0)
            except Exception as exc:
                logger.info("Grok leader initialize failed: %s", exc)
                await self.close()
                self._note_connect_failure(identity)
                return False
            try:
                await self._notify("notifications/initialized", {})
            except OSError:
                await self.close()
                self._note_connect_failure(identity)
                return False
            self.connected = True
            self._fail_identity = None
            self._fail_at = 0.0
            await self._discover(result)
            return True
        finally:
            self._connecting = False

    async def _discover(self, init_result) -> None:
        advertised = isinstance(init_result, dict)
        methods = (
            [m for m in (SESSION_LIST, SESSION_LIST_XAI) if _advertises(init_result, m)]
            if advertised else []
        )
        if not methods:
            # Initialize did not name a list method. Try the two we know; if
            # neither can distinguish live from disk, stay empty.
            methods = [SESSION_LIST, SESSION_LIST_XAI]
        self._list_methods = methods
        if await self._relist_residents():
            return
        self._log_honest_empty(
            "Grok leader cannot distinguish live sessions from disk history")

    async def _relist_residents(self) -> bool:
        """Ask the leader outright who is resident. True if it could answer.

        The authority behind `_census_sessions`' ambiguous case: a one-entry
        `sessions` push is shaped exactly like a one-entry upsert, and no
        amount of reading the payload can separate "only this session is left"
        from "here is news about this session". Guessing "upsert" leaves a
        departed session resident, which is what made a reply to a closed tab
        report `ok: true`; guessing "census" drops every other live row. So we
        stop guessing and re-list.
        """
        for method in getattr(self, "_list_methods", None) or []:
            try:
                result = await self._request(method, {}, timeout=5.0)
            except Exception:
                continue
            entries = sessions_from_payload(result)
            if list_has_resident_flag(entries):
                self._set_residents(resident_ids(entries))
                return True
            if entries:
                self._log_honest_empty(
                    "Grok session list has no resident flag; "
                    "waiting for %s", SESSIONS_CHANGED)
                return True
        return False

    def _log_honest_empty(self, message: str, *args) -> None:
        if self._honest_empty_logged:
            return
        self._honest_empty_logged = True
        logger.info(message, *args)

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        over_limit_logged = False
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:
                    # One line over the stream limit. readline() has already
                    # advanced past it, so skip the line rather than killing
                    # the reader (which forced a reconnect every 30s forever).
                    if not over_limit_logged:
                        over_limit_logged = True
                        logger.warning(
                            "Grok leader sent a line over the read limit; "
                            "skipping it")
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        finally:
            if not self._closing:
                await self.close()

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None and ("result" in msg or "error" in msg):
            try:
                key = int(mid)
            except (TypeError, ValueError):
                key = mid
            fut = self._pending.pop(key, None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_result(("error", msg.get("error")))
                else:
                    fut.set_result(("ok", msg.get("result")))
            return

        method = msg.get("method") or ""
        if method == SESSIONS_CHANGED or method.endswith("sessions/changed"):
            # A push may be a delta (upserted/removed, or a one-entry
            # sessions list). Replacing the whole set from one entry
            # dropped every other live client and hid Reply.
            self._set_residents(merge_sessions_changed(self.residents, msg))
            # …and merging it as a delta is the other half of that coin: on the
            # commonest transition of all, two tabs down to one, the departed
            # session stays resident forever. Neither reading is safe, so the
            # ambiguous shape triggers an authoritative re-list. Off this
            # handler, which must not block the reader loop on a request whose
            # reply arrives through it.
            if _ambiguous_census(_session_fields(msg)):
                self._schedule_relist()
            return

        if method == QUEUE_CHANGED or method.endswith("queue/changed"):
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            sid = session_id_of(params)
            if not sid and isinstance(params.get("session"), dict):
                sid = session_id_of(params["session"])
            if sid:
                waiters = self._prompt_acks.pop(sid, [])
                for fut in waiters:
                    if not fut.done():
                        fut.set_result(True)

    async def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise OSError("not connected")
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float = 5.0):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("not connected")
        rid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        await self._proc.stdin.drain()
        try:
            kind, body = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise
        if kind == "error":
            raise RuntimeError(body)
        return body

    async def prompt(self, session_id: str, text: str) -> tuple[bool, str]:
        """Queue a follow-up. Returns once the leader accepted the write.

        Does **not** await the JSON-RPC result of the whole turn — that can
        take minutes, and the caller sits on the daemon loop.
        """
        if not self.connected or self._proc is None or self._proc.stdin is None:
            return False, ("The Grok leader is not running, "
                           "so this session cannot be reached from here.")
        rid = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        rpc_fut: asyncio.Future = loop.create_future()
        self._pending[rid] = rpc_fut
        ack_fut: asyncio.Future = loop.create_future()
        self._prompt_acks.setdefault(session_id, []).append(ack_fut)
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        }
        try:
            self._proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            await self._proc.stdin.drain()
        except OSError:
            self._pending.pop(rid, None)
            acks = self._prompt_acks.get(session_id) or []
            if ack_fut in acks:
                acks.remove(ack_fut)
            return False, "That session is no longer listening."

        done, _pending = await asyncio.wait(
            {rpc_fut, ack_fut},
            timeout=PROMPT_ACK_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # close() cancels these. A cancelled future is `done`, so treating
        # that as success reported a reply that never landed.
        if ack_fut.cancelled() or rpc_fut.cancelled():
            return False, "That session is no longer listening."
        if ack_fut in done:
            return True, ""
        if rpc_fut in done:
            kind, body = rpc_fut.result()
            if kind == "error":
                detail = ""
                if isinstance(body, dict):
                    detail = str(body.get("message") or "")
                elif body:
                    detail = str(body)
                return False, detail or "That session is no longer listening."
            return True, ""
        # Write left the pipe and no error arrived. Same spirit as
        # `_send_to_channel`: the request is pending on the client; do not
        # block the caller on end-of-turn. A session that left the
        # resident set while we waited is not a success.
        if session_id not in self.residents:
            return False, "That session is no longer listening."
        return True, ""

    async def close(self) -> None:
        """Terminate the child and clear state. Idempotent."""
        if self._closing and self._proc is None:
            return
        if self._closing:
            return
        self._closing = True
        self._mark_down()

        relist = self._relist_task
        self._relist_task = None
        if relist is not None and not relist.done():
            relist.cancel()

        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except (ProcessLookupError, OSError):
                pass

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        for waiters in self._prompt_acks.values():
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._prompt_acks.clear()
        self._closing = False
