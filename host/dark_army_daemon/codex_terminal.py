"""Read-only terminal navigation for Codex's shared app-server TUI.

The server owns journals; the native terminal owns the thread's ``codex_tui``
MCP listener. This evidence identifies a terminal; closing additionally requires a freshly
completed turn and a deliberate human request. It never grants Stop permission.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import socket
import stat
from urllib.parse import urlsplit

import psutil
from websockets.asyncio.client import unix_connect

from . import codex_rollouts as rollouts

REFRESH_SECONDS = 15.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Root:
    session_id: str
    thread_id: str
    path: Path
    cwd: str


@dataclass(frozen=True)
class Target:
    root: Root
    pid: int
    executable: str
    created: float
    argv: tuple[str, ...]
    tty: str
    port: int


def roots(records) -> tuple[Root, ...]:
    # 0.157's local daemon TUI records source=vscode. Do not widen the old
    # CLI/control classifier: an IDE journal alone still grants nothing.
    # Activity changes roster order without changing terminal identity.
    return tuple(sorted((Root(r.session_id, r.thread_id, Path(r.path), r.cwd)
                 for r in records if not r.is_child and r.originator == "codex-tui"
                 and r.source_kind == "vscode" and r.thread_source == "user"),
                        key=lambda root: root.session_id))


def _socket_identity(path):
    info = path.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("not our local control socket")
    return info.st_dev, info.st_ino


def _server_identity(sock):
    # Darwin LOCAL_PEERPID on a Unix socket: verify the actual peer, not a
    # pid file or the ancestor which happened to start the shared server.
    pid = sock.getsockopt(0, 2)
    proc = psutil.Process(pid)
    exe, argv = os.path.realpath(proc.exe()), tuple(proc.cmdline())
    if (Path(exe).name != "codex" or ".app/contents/" in exe.lower()
            or len(argv) < 2 or argv[1] != "app-server"
            or proc.uids().real != os.getuid()):
        raise ValueError("not a native local Codex server")
    return pid, exe, proc.create_time(), argv


def _port(reply):
    entries = [x for x in reply if x.get("name") == "codex_tui"]
    if len(entries) != 1 or entries[0].get("runtimeStatus") != "connected":
        return None
    origin = entries[0].get("httpOrigin")
    if not isinstance(origin, str):
        return None
    parsed = urlsplit(origin)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        return None
    return parsed.port


def _listeners():
    observed = rollouts._process_snapshot()
    if observed is None:
        return {}
    found = {}
    for candidate in rollouts._title_processes(observed):
        if not candidate.native:
            continue
        try:
            proc = psutil.Process(candidate.pid)
            fresh = rollouts._fresh_title_process(proc)
            if fresh is None:
                continue
            for conn in proc.net_connections(kind="tcp"):
                if (conn.status == psutil.CONN_LISTEN and conn.family == socket.AF_INET
                        and conn.laddr.ip == "127.0.0.1"):
                    found.setdefault(conn.laddr.port, []).append(fresh)
        except (OSError, ValueError, psutil.Error):
            continue
    return found


def _match(root, port, listeners):
    candidates = listeners.get(port, ())
    if len(candidates) != 1:
        return None
    p = candidates[0]
    if p.cwd != root.cwd or not p.native or not p.tty:
        return None
    return Target(root, p.pid, p.executable, p.create_time, p.argv, p.tty, port)


async def resolve(captured: tuple[Root, ...], *, recheck=False, stopped_turns=None) -> dict[str, Target]:
    """Bounded read-only observation; Jump bypasses the published reading.

    No server launch, resume, subscription, input, SQLite or process environment
    access. Unavailable/older servers and conflicting terminal owners fail closed.
    """
    if not captured or len(captured) > 64:
        return {}
    control_socket = rollouts.SESSIONS_DIR.parent / "app-server-control/app-server-control.sock"
    try:
        # A fleet-wide MCP status read measured 26s on 0.157 under load.
        # Discovery is detached from snapshots; the human Jump deadline stays
        # short. A timeout still withdraws the entire ambiguous reading.
        async with asyncio.timeout(4.0 if recheck else 30.0):
            identity = await asyncio.to_thread(_socket_identity, control_socket)
            async with unix_connect(control_socket, uri="ws://localhost/",
                                    max_size=8 * 1024 * 1024, close_timeout=0.1) as ws:
                sock = ws.transport.get_extra_info("socket")
                peer = await asyncio.to_thread(_server_identity, sock)
                request_id = 0

                async def request(method, params):
                    nonlocal request_id
                    request_id += 1
                    await ws.send(json.dumps(dict(id=request_id, method=method, params=params)))
                    for _ in range(256):
                        reply = json.loads(await ws.recv())
                        if reply.get("id") == request_id:
                            if "error" in reply:
                                raise ValueError("Codex read refused")
                            return reply["result"]
                    raise ValueError("too many unrelated messages")

                await request("initialize", {
                    "clientInfo": {"name": "dark-army-navigation", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                await ws.send(json.dumps({"method": "initialized"}))

                async def ports():
                    nonlocal request_id
                    result, pending = {}, {}

                    async def send(root, entries, seen, cursor=None):
                        nonlocal request_id
                        request_id += 1
                        params = {"threadId": root.thread_id, "detail": "toolsAndAuthOnly",
                                  "limit": 100}
                        if cursor is not None:
                            params["cursor"] = cursor
                        pending[request_id] = root, entries, seen
                        await ws.send(json.dumps(dict(id=request_id,
                            method="mcpServerStatus/list", params=params)))

                    for root in captured:
                        await send(root, [], set())
                    for _ in range(1024):
                        if not pending:
                            break
                        reply = json.loads(await ws.recv())
                        item = pending.pop(reply.get("id"), None)
                        if item is None:
                            continue
                        root, entries, seen = item
                        if "error" in reply:
                            continue  # an unloaded thread cannot poison other terminals
                        page = reply["result"]
                        entries.extend(page["data"])
                        cursor = page.get("nextCursor")
                        if cursor is None:
                            result[root.session_id] = _port(entries)
                        elif isinstance(cursor, str) and cursor not in seen and len(seen) < 7:
                            seen.add(cursor)
                            await send(root, entries, seen, cursor)
                    return result

                before_ports = await ports()
                before = await asyncio.to_thread(_listeners)
                after = await asyncio.to_thread(_listeners)
                if stopped_turns is not None:
                    for root in captured:
                        if (root.session_id in stopped_turns and not await _server_stopped(
                                request, root, stopped_turns[root.session_id])):
                            return {}
                if (identity != await asyncio.to_thread(_socket_identity, control_socket)
                        or peer != await asyncio.to_thread(_server_identity, sock)):
                    return {}
                result = {}
                for root in captured:
                    port = before_ports.get(root.session_id)
                    target = _match(root, port, before)
                    if target is not None and target == _match(root, port, after):
                        result[root.session_id] = target
                # Multiple displayed threads in one TUI do not identify which
                # one Jump would show. Do not borrow its terminal for either.
                return {sid: t for sid, t in result.items()
                        if sum(v.pid == t.pid or v.tty == t.tty
                               for v in result.values()) == 1}
    except Exception:
        # Observation must never take down the fleet. Cancellation propagates.
        logger.debug("Codex terminal observation unavailable", exc_info=True)
        return {}


async def _server_stopped(request, root, turn_id):
    """Read the addressed thread's latest turn; never resume or subscribe."""
    if not turn_id:
        return False
    thread = (await request("thread/read", {
        "threadId": root.thread_id, "includeTurns": False})).get("thread", {})
    if (thread.get("id") != root.thread_id or thread.get("path") != str(root.path)
            or thread.get("cwd") != root.cwd or thread.get("parentThreadId") is not None
            or thread.get("originator") != "codex-tui" or thread.get("source") != "vscode"
            or thread.get("threadSource") != "user"
            or thread.get("status") != {"type": "idle"}):
        return False
    turns = (await request("thread/turns/list", {
        "threadId": root.thread_id, "limit": 1, "sortDirection": "desc"})).get("data")
    return (isinstance(turns, list) and len(turns) == 1
            and turns[0].get("id") == turn_id and turns[0].get("status") == "completed")


def journal_revisions(root, children):
    try:
        paths = (root.path,) + tuple(path for _tid, path, _parent, _turn in children)
        return tuple((s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size)
                     for s in (path.stat() for path in paths))
    except OSError:
        return None


def stopped_journals(root, turn_id, children):
    """Fresh root and entire retained helper tree, pinned across parsing."""
    before = journal_revisions(root, children)
    if before is None:
        return None
    parsed = []
    for child_id, path, parent_id, child_turn in children:
        child = rollouts.parse_rollout(path)
        if (child is None or child.thread_id != child_id
                or child.parent_thread_id != parent_id or child.turn_id != child_turn
                or not rollouts.stopped_turn(child)):
            return None
        parsed.append(child)
    record = rollouts.parse_rollout(root.path)
    if (record is None or roots((record,)) != (root,) or record.turn_id != turn_id
            or not rollouts.stopped_turn(record)):
        return None
    finished = {child.thread_id for child in parsed}
    for item in (record, *parsed):
        if (rollouts.has_unsettled_path_children(item, parsed)
                or any(a.activity not in ("completed", "shutdown", "errored")
                       and aid not in finished for aid, a in item.stats.agents.items())):
            return None
    return before if journal_revisions(root, children) == before else None
