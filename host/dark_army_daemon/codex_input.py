"""Addressed replies to already-loaded shared-server Codex TUI threads.

This is input authority from the server, independent of terminal navigation.
Never starts/resumes a thread, changes its settings, or sends terminal keys.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import time

from websockets.asyncio.client import unix_connect

from . import codex_rollouts, codex_terminal

REFRESH_SECONDS = 15.0
MAX_AGE_SECONDS = 30.0


@dataclass(frozen=True)
class Candidate:
    root: codex_terminal.Root
    turn_id: str
    active: bool
    question_id: str


@dataclass(frozen=True)
class Proof:
    candidate: Candidate
    peer: tuple
    socket: tuple
    observed_at: float


def candidate(record):
    if record is None or not (roots := codex_terminal.roots((record,))):
        return None
    question = record.stats.question or {}
    if (not record.journal_complete or not record.turn_observed or not record.turn_id
            or codex_rollouts.question_blocks(record, awaiting_answer=True)):
        return None
    if record.turn_active:
        if not record.question_async or not question.get("id"):
            return None
    elif not codex_rollouts.stopped_turn(record, awaiting_answer=True):
        return None
    return Candidate(roots[0], record.turn_id, record.turn_active, question.get("id", ""))


@asynccontextmanager
async def connection():
    path = codex_rollouts.SESSIONS_DIR.parent / "app-server-control/app-server-control.sock"
    identity = await asyncio.to_thread(codex_terminal._socket_identity, path)
    async with unix_connect(path, uri="ws://localhost/", max_size=1024 * 1024,
                            close_timeout=0.1) as ws:
        sock = ws.transport.get_extra_info("socket")
        peer = await asyncio.to_thread(codex_terminal._server_identity, sock)
        serial = 0

        async def request(method, params, before_send=None):
            nonlocal serial
            serial += 1
            raw = json.dumps(dict(id=serial, method=method, params=params))
            if before_send is not None and not before_send():
                raise ValueError("reply changed before send")
            await ws.send(raw)
            for _ in range(256):
                reply = json.loads(await ws.recv())
                if reply.get("id") == serial:
                    if "error" in reply:
                        raise ValueError("Codex request refused")
                    return reply["result"]
            raise ValueError("too many unrelated messages")

        async def unchanged():
            return (identity == await asyncio.to_thread(codex_terminal._socket_identity, path)
                    and peer == await asyncio.to_thread(codex_terminal._server_identity, sock))

        await request("initialize", {
            "clientInfo": {"name": "dark-army-replies", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await ws.send(json.dumps({"method": "initialized"}))
        yield request, unchanged, peer, identity


async def available(request, expected):
    root = expected.root
    result = await request("thread/read", {"threadId": root.thread_id, "includeTurns": False})
    thread = result["thread"]
    status = thread.get("status", {})
    # Exact root metadata AND explicit input authority. An unloaded thread,
    # an IDE/app root, a child or a picker/approval never gains this route.
    if (thread.get("id") != root.thread_id or thread.get("path") != str(root.path)
            or thread.get("cwd") != root.cwd or thread.get("parentThreadId") is not None
            or thread.get("originator") != "codex-tui" or thread.get("source") != "vscode"
            or thread.get("threadSource") != "user"
            or thread.get("canAcceptDirectInput") is not True
            or status.get("type") != ("active" if expected.active else "idle")
            or status.get("activeFlags", []) != []):
        return False
    page = await request("thread/turns/list", {
        "threadId": root.thread_id, "limit": 1, "sortDirection": "desc",
    })
    turns = page["data"]
    return (len(turns) == 1 and turns[0].get("id") == expected.turn_id
            and turns[0].get("status") == ("inProgress" if expected.active else "completed"))


async def observe(candidates):
    if not candidates or len(candidates) > 64:
        return {}
    found = {}
    try:
        async with asyncio.timeout(8.0):
            async with connection() as (request, unchanged, peer, identity):
                for expected in candidates:
                    try:
                        if await available(request, expected):
                            found[expected.root.session_id] = Proof(
                                expected, peer, identity, time.monotonic())
                    except (ValueError, KeyError, TypeError):
                        continue
                return found if await unchanged() else {}
    except Exception:
        return {}


async def submit(proof, text, current, consume):
    """One write at most. A lost acknowledgement is not retried or typed."""
    expected = proof.candidate
    sent = False
    try:
        async with asyncio.timeout(8.0):
            async with connection() as (request, unchanged, peer, identity):
                if peer != proof.peer or identity != proof.socket or not current():
                    return False, "The Codex server changed. Nothing was sent."
                if not await available(request, expected):
                    return False, "The Codex turn changed or is not accepting input. Nothing was sent."
                fresh = await asyncio.to_thread(codex_rollouts.parse_rollout, expected.root.path)
                if candidate(fresh) != expected or not await unchanged():
                    return False, "The Codex question or session changed. Nothing was sent."
                # Read again after journal work; do not spend a stale capability.
                if not await available(request, expected) or not await unchanged():
                    return False, "The Codex turn changed. Nothing was sent."

                def before_send():
                    nonlocal sent
                    if not current() or not consume():
                        return False
                    sent = True
                    return True

                params = {"threadId": expected.root.thread_id,
                          "input": [{"type": "text", "text": text, "text_elements": []}]}
                if expected.active:
                    params["expectedTurnId"] = expected.turn_id
                result = await request("turn/steer" if expected.active else "turn/start",
                                       params, before_send)
                confirmed = (result.get("turnId") == expected.turn_id if expected.active
                             else bool(result.get("turn", {}).get("id")))
                if confirmed:
                    return True, ""
    except Exception:
        pass
    if sent:
        return False, "Codex did not confirm the reply. Check the original session; no retry was sent."
    return False, "The Codex connection could not be verified. Nothing was sent."
