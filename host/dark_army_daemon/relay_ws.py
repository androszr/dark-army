# host/dark_army_daemon/relay_ws.py
"""The away link's fast lane: one WebSocket per paired phone, held open to
the socket relay (``relay-ws/``) beside the mailbox connector.

``relay_client.RelayConnector``'s shape, on the same daemon loop, with the
same key, the same ``RELAY`` namespace and the same counters — a frame
captured from the mailbox and replayed into the socket (or the other way)
is refused by the shared monotonic counter, and nothing new is persisted on
either end. The Mac reaches *out*; nothing new listens.

What the socket adds over the mailbox is one thing: the Mac **pushes** the
picture the moment it changes (`nudge`, hung on ``ApiServer.on_picture``),
and only down a socket the phone has **armed** — its first frame that
``relay.open_frame`` verifies on *this* socket. A stranger holding the
channel id can connect and receive nothing, exactly as today's mailbox
hands a stranger nothing but the chance to post ciphertext; the arming is
cleared on every close.

The four bounds, in order: the per-device frame bucket runs before any
decryption, the write bucket before any execution (both ``relay.py``'s),
the push bucket (`WS_PUSH_MAX_PER_MINUTE`) before any push, and the
reconnect ladder (`WS_RECONNECT_START` doubling to `WS_RECONNECT_MAX`,
reset only after `WS_STABLE_SECONDS` open; a ``4001 replaced`` waits the
maximum) so a dead or refusing relay is never a hot loop. Every refused
frame goes to the access log under door ``ws`` and every trip is timed on
the same door (`link_timing.py`: ``open`` / ``run`` / ``answer`` / ``size``).
The mailbox connector is untouched and keeps its own pacing: socket frames
do **not** stamp ``relay.last_frame_at``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import websockets
from websockets.exceptions import InvalidStatus

from . import api_server, devices, relay
from .relay_client import (
    HEALTH_REPORT_SECONDS, SUPERVISE_SECONDS, RelayConnector, _Bucket,
    _Health, _WRITE_LIMIT_REFUSAL,
)

logger = logging.getLogger("dark-army")

#: The one door name this module files under (`access_log.DOORS`).
DOOR = "ws"
#: Ping / pong on the line; a missed pong closes it and the ladder reopens.
WS_PING_SECONDS = 20.0
#: The reconnect ladder: doubling from the first to the second, reset only
#: after the line has been open `WS_STABLE_SECONDS`; a relay that accepts
#: and closes at once therefore climbs the ladder rather than spinning.
WS_RECONNECT_START = 1.0
WS_RECONNECT_MAX = 60.0
WS_STABLE_SECONDS = 30.0
#: How long a missing or refused address waits before it is re-read.
WS_URL_RETRY_SECONDS = 5.0
#: Pushes coalesce: `nudge` books one `_push_all` this long after the first
#: nudge and ignores the rest until it has run — `_broadcast` fires several
#: times a second on a busy fleet.
WS_PUSH_MIN_INTERVAL = 0.5
#: Pushes per device per minute, taken before `state()` is built.
WS_PUSH_MAX_PER_MINUTE = 60
#: The relay's `4001 replaced`: another Mac took this channel — wait the
#: maximum rather than fight it.
CLOSE_REPLACED = 4001
#: The largest frame the socket will take: the sealed frame cap plus room.
WS_MAX_SIZE = relay.RELAY_FRAME_MAX_BYTES + 1024
#: `health_snapshot`'s `state` while the switch is on but the lane cannot
#: even be tried: no socket address stored (`HEALTH_NO_ADDRESS`) or a stored
#: address the connector refuses (`HEALTH_BAD_ADDRESS`, only reachable by
#: editing `relay.json` by hand — `set_ws_url` refuses it). The panel draws
#: each as its own sentence; the five key names stay `relay_health`'s.
HEALTH_NO_ADDRESS = "no-address"
HEALTH_BAD_ADDRESS = "bad-address"
#: The words `devices_snapshot` publishes per phone as `socket`.
SOCKET_OFF, SOCKET_CONNECTING, SOCKET_OPEN, SOCKET_ARMED = (
    "off", "connecting", "open", "armed")
SOCKET_WORDS = (SOCKET_OFF, SOCKET_CONNECTING, SOCKET_OPEN, SOCKET_ARMED)
#: The reply's `re` field is empty on a counter refusal (`relay_client`'s
#: rule): with requests serialised on the phone, the current waiter is the
#: only one it can mean.
_HEALTH_SUBJECT = "relay socket"
_HEALTH_CONSEQUENCE = ("the socket lane is down until this clears; the "
                       "mailbox carries the phone")

#: The sleep the ladder takes. A module name rather than `asyncio.sleep`
#: inline so a test can stand a counter in without patching asyncio itself.
_sleep = asyncio.sleep


def _ws_url_ok(url: str) -> bool:
    """``wss://`` only, with one stated exception: plain ``ws://`` to
    loopback, the test seam (a fake relay on 127.0.0.1) that cannot leak on
    a network. ``relay.set_ws_url`` already refuses storing anything else."""
    if url.startswith("wss://"):
        return True
    return url.startswith("ws://127.0.0.1") or url.startswith("ws://localhost")


@dataclass(frozen=True)
class _Hops:
    """The one leg `_handle_wire` measures before a request runs: the seal's
    opening. `_execute` adds `run` and `answer` and files the record."""
    open: float = 0.0


class RelaySocketConnector:
    """Owns one task per channel and the sockets they hold. Built once by
    the daemon, started and stopped on the loop by `set_relay_ws` /
    `set_remote_access`."""

    def __init__(self, api):
        self._api = api
        self._tasks: dict[str, asyncio.Task] = {}
        #: In-flight request handlers and pushes, one task each.
        self._handlers: set[asyncio.Task] = set()
        self._supervisor: asyncio.Task | None = None
        self._frame_buckets: dict[str, _Bucket] = {}
        self._write_buckets: dict[str, _Bucket] = {}
        self._push_buckets: dict[str, _Bucket] = {}
        #: Per-device ordering locks for `_answer`: ctr allocation and the
        #: send are one step, `relay_client._answer`'s reason.
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        #: The open socket per device, present only between the handshake
        #: and the close. Loop-owned.
        self._sockets: dict = {}
        #: Devices whose open socket has carried one verified frame. Cleared
        #: on every close; `_push_all` reads it at the moment of sending.
        self._armed: set[str] = set()
        #: Devices whose channel task is between attempts.
        self._connecting: set[str] = set()
        #: The `state_digest` last handed down each socket — by a push or by
        #: the phone's own `state` answer — so a push after nothing changed
        #: sends nothing.
        self._pushed_digest: dict[str, str] = {}
        #: The one booked push, or None. `nudge` books, `_push_booked` clears.
        self._push_handle: Optional[asyncio.TimerHandle] = None
        #: Refused frames by refusal code — a count, never a body.
        self.dropped: dict[str, int] = {}
        #: Whether the one "no socket address" warning has been written for
        #: the current spell without an address; reset once one is seen.
        self._warned_no_address = False
        #: Per-device socket standing. Loop-owned; see `health_snapshot`.
        self._health: dict[str, _Health] = {}
        #: Set while `stop()` is awaiting the cancelled tasks; a `start()`
        #: arriving then is remembered and run when the stop completes, so
        #: a quick off→on never has its fresh sockets wiped by the old stop.
        self._stopping: Optional[asyncio.Future] = None
        self._restart_wanted = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """On the loop. Idempotent — a second enable is a no-op. A start
        that lands while `stop()` is still awaiting the old tasks is
        deferred until that stop returns, never interleaved with it."""
        if self._running:
            return
        if self._stopping is not None:
            self._restart_wanted = True
            return
        self._running = True
        self._supervisor = asyncio.get_running_loop().create_task(
            self._supervise())
        logger.info("relay socket connector started")

    async def stop(self) -> None:
        """On the loop. Cancels every task, which closes every socket.

        Every container is cleared **before** the cancelled tasks are
        awaited, and a second `stop()` arriving meanwhile waits for the
        first; a `start()` arriving meanwhile is run once this returns.
        Wiping the containers after the awaits was a race: a quick off→on
        of Socket link or Away access had the fresh connector's sockets
        wiped from the containers while still connected."""
        if self._stopping is not None:
            # A stop is already in flight: this one cancels any restart
            # a start in between asked for, and waits for it.
            self._restart_wanted = False
            await self._stopping
            return
        if not self._running and self._supervisor is None:
            return
        self._running = False
        self._stopping = asyncio.get_running_loop().create_future()
        try:
            if self._push_handle is not None:
                self._push_handle.cancel()
                self._push_handle = None
            pending = [t for t in (self._supervisor, *self._tasks.values(),
                                   *self._handlers)
                       if t is not None]
            self._supervisor = None
            self._tasks = {}
            self._handlers = set()
            self._sockets = {}
            self._armed = set()
            self._connecting = set()
            self._pushed_digest = {}
            # Reset so a restarted connector can say "failing" afresh.
            self._health = {}
            self._warned_no_address = False
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            stopping, self._stopping = self._stopping, None
            if not stopping.done():
                stopping.set_result(None)
        logger.info("relay socket connector stopped")
        if self._restart_wanted:
            self._restart_wanted = False
            self.start()

    # ── supervision ───────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Keep one task per stored channel, at most ``MAX_DEVICES``."""
        try:
            while self._running:
                wanted = list(relay.channel_ids())[:devices.MAX_DEVICES]
                for did in wanted:
                    task = self._tasks.get(did)
                    if task is None or task.done():
                        self._tasks[did] = asyncio.get_running_loop() \
                            .create_task(self._serve(did))
                for did in list(self._tasks):
                    if did not in wanted:
                        self._tasks.pop(did).cancel()
                await asyncio.sleep(SUPERVISE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay socket supervisor failed", exc_info=True)

    # ── one channel ───────────────────────────────────────────────────────────

    async def _serve(self, device_id: str) -> None:
        """One device's line: connect, hold, receive, reconnect on the
        ladder. Never dies silently — every failure is a health record and
        a ladder step."""
        backoff = WS_RECONNECT_START
        try:
            while self._running:
                key = relay.channel_key(device_id)
                if key is None:
                    return  # forgotten mid-run: the line goes silent
                url = relay.get_ws_url()
                if not url or not _ws_url_ok(url):
                    # One line for the whole spell, `_note_no_address`'s;
                    # a second "address refused" line here was two
                    # WARNINGs for one event.
                    self._connecting.discard(device_id)
                    self._note_no_address(
                        device_id,
                        HEALTH_BAD_ADDRESS if url else HEALTH_NO_ADDRESS)
                    await _sleep(WS_URL_RETRY_SECONDS)
                    continue
                self._warned_no_address = False
                chan = relay.channel_id(key)
                target = f"{url}/ws?ch={chan}&side=mac"
                self._connecting.add(device_id)
                opened_at: float | None = None
                close_code: int | None = None
                failed_status: int | None = None
                ws = None
                try:
                    async with websockets.connect(
                            target, ping_interval=WS_PING_SECONDS,
                            ping_timeout=WS_PING_SECONDS,
                            max_size=WS_MAX_SIZE, origin=None) as ws:
                        opened_at = time.monotonic()
                        self._sockets[device_id] = ws
                        self._note_ok(device_id)
                        self._picture_changed()
                        try:
                            async for message in ws:
                                if isinstance(message, bytes):
                                    continue  # text frames only
                                text = message.strip()
                                if text.startswith("peer:"):
                                    # The relay's own word — the other side
                                    # arrived (`peer:1`) or left (`peer:0`) —
                                    # never opened, and it can only narrow:
                                    # whoever verified before is no longer
                                    # the one on the line (a `peer:1` after
                                    # arming is a displacing socket), so
                                    # nothing is pushed until a fresh
                                    # verified request re-arms it.
                                    self._disarm(device_id)
                                    continue
                                try:
                                    await self._handle_wire(device_id, key, text)
                                except asyncio.CancelledError:
                                    raise
                                except Exception:
                                    logger.warning(
                                        "relay socket frame handling failed",
                                        exc_info=True)
                        finally:
                            close_code = ws.close_code
                except asyncio.CancelledError:
                    raise
                except InvalidStatus as exc:
                    failed_status = int(getattr(
                        getattr(exc, "response", None), "status_code", 0) or 0)
                except Exception:
                    failed_status = 0
                finally:
                    # Only this task's own socket is taken down: a stop()
                    # awaiting this task may already have let a restarted
                    # connector open a fresh line for the same device.
                    if ws is not None and self._sockets.get(device_id) is ws:
                        self._sockets.pop(device_id, None)
                        self._armed.discard(device_id)
                        self._picture_changed()
                if not self._running:
                    return
                held = (time.monotonic() - opened_at) if opened_at else 0.0
                if close_code == CLOSE_REPLACED:
                    # Another Mac took this channel: wait the maximum rather
                    # than fight it, and say so through the health record.
                    self._note_failure(device_id, CLOSE_REPLACED)
                    wait = WS_RECONNECT_MAX
                    backoff = WS_RECONNECT_START
                else:
                    if failed_status is not None:
                        self._note_failure(device_id, failed_status)
                    elif held < WS_STABLE_SECONDS:
                        # Accepted and dropped inside the stable window: a
                        # failure for the ladder, whatever the close code.
                        self._note_failure(device_id, int(close_code or 0))
                    if held >= WS_STABLE_SECONDS:
                        backoff = WS_RECONNECT_START
                    wait = backoff
                    backoff = min(WS_RECONNECT_MAX, backoff * 2.0)
                await _sleep(wait)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay socket channel task failed", exc_info=True)
        finally:
            self._connecting.discard(device_id)

    async def _handle_wire(self, device_id: str, key: bytes, wire: str) -> None:
        """One frame off the line: buckets, seal and counters **inline** (in
        arrival order, persist-before-execute), the run and its answer as
        their own task. `relay_client._handle_wire` without the mailbox's
        two legs; the key is re-read per frame so `forget` bites between
        two frames of one open line."""
        if not self._frame_bucket(device_id).take():
            self._drop("rate", device_id)
            return
        fresh = relay.channel_key(device_id)
        if fresh is None:
            return
        key = fresh
        last = relay.last_recv_ctr(device_id)
        opened_at = time.monotonic()
        frame, err = relay.open_frame(key, relay.DIR_PHONE_TO_MAC, wire, last)
        open_seconds = time.monotonic() - opened_at
        if err == "ctr":
            # The one refusal worth answering: sealed to the phone, so the
            # counter is handed only to the other keyholder. A stale ctr
            # arms nothing — a captured frame replayed here verifies its
            # seal too.
            self._drop(err, device_id)
            await self._answer(device_id, key, {
                "re": "", "status": 409,
                "error": relay.REFUSAL_WORDS[err],
                "ctr_expected": last + 1,
            }, kind="err")
            return
        if err:
            self._drop(err, device_id)
            return
        kind = str(frame.get("kind") or "")
        # The shared counter, without the mailbox's liveness stamp: the
        # mailbox connector keeps its own pacing (`relay.note_recv_ctr`).
        relay.note_recv_ctr(device_id, int(frame["ctr"]),
                            durable=(kind == "action"), liveness=False)
        if device_id not in self._armed and device_id in self._sockets:
            # The first verified frame on this line arms it: from here the
            # Mac may push the picture down it.
            self._armed.add(device_id)
            self._picture_changed()
        payload = frame.get("body")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "action" and not self._write_bucket(device_id).take():
            await self._answer(device_id, key, {
                "re": str(frame.get("id") or ""), "status": 429,
                "error": _WRITE_LIMIT_REFUSAL,
            }, kind="err")
            return
        task = asyncio.get_running_loop().create_task(
            self._execute(device_id, key, kind, payload,
                          str(frame.get("id") or ""),
                          _Hops(open=max(0.0, open_seconds))))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _execute(self, device_id: str, key: bytes, kind: str,
                       payload: dict, frame_id: str,
                       hops: Optional[_Hops] = None) -> None:
        """One verified request's run and sealed answer — its own task, off
        the line's receive loop. The reply envelope carries `timing`
        (`open` / `run`) beside `body`, never inside it; the record goes
        to door `ws` after the answer has gone, `_drop`'s way."""
        hops = hops or _Hops()
        try:
            run_at = time.monotonic()
            status, ctype, out = await self._api._remote_run(
                kind, payload, device_id)
            run_seconds = time.monotonic() - run_at
            if kind == "state" and int(status) == 200 \
                    and payload.get("done") == "review":
                # What the phone now holds is what a push compares against
                # — the review-only board is the picture `_push_all` asks
                # for, so only that answer's digest is remembered.
                self._remember_digest(device_id, out)
            answer_at = time.monotonic()
            sent = await self._answer(device_id, key, {
                "re": frame_id,
                "status": int(status),
                "content_type": str(ctype),
                "body": out.decode("utf-8", "replace"),
                "timing": {"open": hops.open, "run": run_seconds},
            }, kind="reply")
            answer_seconds = time.monotonic() - answer_at
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay socket request handling failed",
                           exc_info=True)
            return
        self._note_timing(device_id, kind, {
            "open": hops.open, "run": run_seconds, "answer": answer_seconds,
        }, int(status) if sent else 0, len(out))

    async def _answer(self, device_id: str, key: bytes, body: dict,
                      *, kind: str) -> bool:
        """Seal and send one Mac-to-phone frame down this device's open
        line. True when the send went through; False when there is no line
        or it failed — the timing record files that under status 0."""
        ws = self._sockets.get(device_id)
        if ws is None:
            return False
        # Allocation and send under one lock per device: the phone's
        # `recvCtr` only goes up, so a higher ctr landing first turns the
        # lower one into a "replay".
        async with self._send_lock(device_id):
            ctr = relay.next_send_ctr(device_id)
            if ctr <= 0:
                return False
            wire = relay.seal_frame(key, relay.DIR_MAC_TO_PHONE, ctr, kind,
                                    body)
            if not wire and kind in ("reply", "push"):
                # The answer outgrew the frame cap: say so in a small err
                # the phone can open. A fresh counter, never reuse.
                ctr = relay.next_send_ctr(device_id)
                if ctr <= 0:
                    return False
                wire = relay.seal_frame(
                    key, relay.DIR_MAC_TO_PHONE, ctr, "err", {
                        "re": str(body.get("re") or ""), "status": 500,
                        "error": relay.REFUSAL_WORDS["oversize"],
                    })
            if not wire:
                return False
            ws = self._sockets.get(device_id)
            if ws is None:
                return False
            try:
                await ws.send(wire)
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            return True

    # ── the push ──────────────────────────────────────────────────────────────

    def nudge(self) -> None:
        """The picture changed (`ApiServer.on_picture`). Books one
        `_push_all` `WS_PUSH_MIN_INTERVAL` out when none is booked and
        does nothing else — this runs inside `_broadcast`, several times a
        second on a busy fleet. Nothing off the loop."""
        if not self._running or self._push_handle is not None:
            return
        if not self._armed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._push_handle = loop.call_later(WS_PUSH_MIN_INTERVAL,
                                            self._push_booked)

    def _push_booked(self) -> None:
        self._push_handle = None
        if not self._running:
            return
        task = asyncio.get_running_loop().create_task(self._push_all())
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _push_all(self) -> None:
        """One `state` answer per armed device, sent only when it changed
        against the digest last handed down that line. Never broadcasts:
        the picture is what changed, not the Devices menu.

        The picture and its digests are built **once per push**, not once
        per phone: a device whose last-handed digest already matches is
        skipped before its bucket is taken (nothing is sent, so nothing is
        spent), and the rest are answered off the same triple."""
        if not any(d in self._sockets for d in self._armed):
            return
        try:
            picture = self._api.state(done_review=True)
            whole, sections = api_server._state_digests(picture)
        except Exception:
            logger.warning("relay socket push failed", exc_info=True)
            return
        built = (picture, whole, sections)
        for device_id in list(self._armed):
            if device_id not in self._armed or device_id not in self._sockets:
                continue
            if self._pushed_digest.get(device_id) == whole:
                continue
            key = relay.channel_key(device_id)
            if key is None:
                continue
            if not self._push_bucket(device_id).take():
                continue
            try:
                run_at = time.monotonic()
                status, ctype, out = await self._api._remote_run("state", {
                    "done": "review",
                    "digest": self._pushed_digest.get(device_id, ""),
                    "with_usage": True,
                }, device_id, prebuilt=built)
                run_seconds = time.monotonic() - run_at
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("relay socket push failed", exc_info=True)
                continue
            if int(status) != 200:
                continue
            try:
                answer = json.loads(out)
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get("unchanged") is True:
                continue
            digest = str(answer.get("state_digest") or "")
            answer_at = time.monotonic()
            sent = await self._answer(device_id, key, {
                "status": int(status),
                "content_type": str(ctype),
                "body": out.decode("utf-8", "replace"),
            }, kind="push")
            answer_seconds = time.monotonic() - answer_at
            if sent:
                self._pushed_digest[device_id] = digest
            self._note_timing(device_id, "push", {
                "run": run_seconds, "answer": answer_seconds,
            }, int(status) if sent else 0, len(out))

    def _remember_digest(self, device_id: str, out: bytes) -> None:
        """The `state_digest` of a `state` answer the phone was just handed
        down this line, so the next push has nothing to say until the
        picture moves past it."""
        try:
            answer = json.loads(out)
        except ValueError:
            return
        if isinstance(answer, dict):
            digest = str(answer.get("state_digest") or "")
            if digest:
                self._pushed_digest[device_id] = digest

    # ── health and words ──────────────────────────────────────────────────────

    def _health_for(self, device_id: str) -> _Health:
        health = self._health.get(device_id)
        if health is None:
            health = _Health()
            self._health[device_id] = health
        return health

    def _note_ok(self, device_id: str) -> None:
        RelayConnector._record_ok(self._health_for(device_id),
                                  subject=_HEALTH_SUBJECT)

    def _note_failure(self, device_id: str, status: int) -> None:
        health = self._health_for(device_id)
        if health.state in (HEALTH_NO_ADDRESS, HEALTH_BAD_ADDRESS):
            # An address arrived and the line is down: the spell without
            # one is over, and the record is reset to `ok` first so
            # `_record_failure`'s ok→failing transition — and its one
            # WARNING — happens instead of the standing staying
            # `no-address` with status 0 while the relay is down.
            health.state = "ok"
            health.status = 0
            health.failures = 0
            health.since = 0.0
            health.last_report = 0.0
        RelayConnector._record_failure(
            health, status, subject=_HEALTH_SUBJECT,
            consequence=_HEALTH_CONSEQUENCE)

    def _note_no_address(self, device_id: str, state: str) -> None:
        """The switch is on but there is no line to try: no socket address
        stored (or one the connector refuses). A standing of its own so
        the Devices menu says so instead of drawing nothing — `status` 0,
        `failures` 0, `failing_for` since the spell began — and **one**
        warning line on the transition, never one per retry."""
        health = self._health_for(device_id)
        if health.state != state:
            health.state = state
            health.status = 0
            health.failures = 0
            health.since = time.monotonic()
            health.last_report = health.since
        if not self._warned_no_address:
            self._warned_no_address = True
            what = ("no socket address set" if state == HEALTH_NO_ADDRESS
                    else "the socket address is refused: "
                         "must start with wss://")
            logger.warning("%s: %s — %s", _HEALTH_SUBJECT, what,
                           _HEALTH_CONSEQUENCE)

    def health_snapshot(self) -> dict:
        """The socket lane's standing, `relay_client.health_snapshot`'s
        keys and its rule: **statuses and clock readings only** — never a
        key, a digest or a channel id. Read off the loop; scalars copied
        in one pass, worst first."""
        records = list(self._health.values())
        if not records:
            return {}
        failing = [h for h in records if h.state == "failing"]
        if failing:
            worst = max(failing, key=lambda h: h.failures)
            return {
                "state": "failing",
                "status": int(worst.status),
                "failures": int(worst.failures),
                "failing_for": max(0.0, time.monotonic() - worst.since),
                "last_ok_at": float(worst.last_ok_at),
            }
        for state in (HEALTH_BAD_ADDRESS, HEALTH_NO_ADDRESS):
            unset = [h for h in records if h.state == state]
            if unset:
                first = min(unset, key=lambda h: h.since)
                return {
                    "state": state,
                    "status": 0,
                    "failures": 0,
                    "failing_for": max(0.0, time.monotonic() - first.since),
                    "last_ok_at": float(max(h.last_ok_at for h in unset)),
                }
        best = max(records, key=lambda h: h.last_ok_at)
        return {
            "state": "ok",
            "status": 0,
            "failures": 0,
            "failing_for": 0.0,
            "last_ok_at": float(best.last_ok_at),
        }

    def socket_word(self, device_id: str) -> str:
        """One of `SOCKET_WORDS` for this phone's line. Read off the loop
        by `devices_snapshot`; set membership only."""
        if device_id in self._armed:
            return SOCKET_ARMED
        if device_id in self._sockets:
            return SOCKET_OPEN
        if device_id in self._connecting:
            return SOCKET_CONNECTING
        return SOCKET_OFF

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _disarm(self, device_id: str) -> None:
        """Take a device's arming away — the line's peer changed under it.
        A transition the Devices menu follows when it was armed."""
        if device_id in self._armed:
            self._armed.discard(device_id)
            self._picture_changed()

    def _picture_changed(self) -> None:
        """A transition (connect, arm, close) the Devices menu follows.
        Under `try` because a test double may not carry `_broadcast`."""
        try:
            self._api._broadcast()
        except Exception:
            logger.debug("broadcast after socket transition skipped",
                         exc_info=True)

    def _note_timing(self, device_id: str, kind: str, hops: dict,
                     status: int, size: int) -> None:
        try:
            self._api.note_link_timing(DOOR, device_id, kind, hops, status,
                                       size)
        except Exception:
            logger.debug("link timing record skipped for ws %s", kind,
                         exc_info=True)

    def _frame_bucket(self, device_id: str) -> _Bucket:
        bucket = self._frame_buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(relay.RELAY_MAX_FRAMES_PER_MINUTE)
            self._frame_buckets[device_id] = bucket
        return bucket

    def _write_bucket(self, device_id: str) -> _Bucket:
        bucket = self._write_buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(relay.RELAY_MAX_WRITES_PER_MINUTE)
            self._write_buckets[device_id] = bucket
        return bucket

    def _push_bucket(self, device_id: str) -> _Bucket:
        bucket = self._push_buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(WS_PUSH_MAX_PER_MINUTE)
            self._push_buckets[device_id] = bucket
        return bucket

    def _send_lock(self, device_id: str) -> asyncio.Lock:
        lock = self._send_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[device_id] = lock
        return lock

    def _drop(self, code: str, device_id: str = "") -> None:
        """Count one refused frame and write it to the access log under
        door `ws`; the peer is the paired device id (the line gives no
        sender address), and the channel id and address are never logged."""
        self.dropped[code] = self.dropped.get(code, 0) + 1
        try:
            self._api._record_access(DOOR, device_id, code,
                                     device_id=device_id)
        except Exception:
            logger.debug("access record skipped for ws %s", code,
                         exc_info=True)


__all__ = [
    "RelaySocketConnector", "DOOR", "SOCKET_WORDS", "SOCKET_OFF",
    "SOCKET_CONNECTING", "SOCKET_OPEN", "SOCKET_ARMED", "WS_PING_SECONDS",
    "WS_RECONNECT_START", "WS_RECONNECT_MAX", "WS_STABLE_SECONDS",
    "WS_PUSH_MIN_INTERVAL", "WS_PUSH_MAX_PER_MINUTE", "CLOSE_REPLACED",
    "HEALTH_REPORT_SECONDS", "HEALTH_NO_ADDRESS", "HEALTH_BAD_ADDRESS",
]
