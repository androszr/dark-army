# host/dark_army_daemon/relay_client.py
"""The away connector: the Mac reaching *out* to the mailbox.

Nothing new listens on the Mac. One asyncio task per channel long-polls the
mailbox for phone-to-Mac envelopes, opens each with ``relay.open_frame``,
runs the verified request through ``ApiServer._remote_run`` on the daemon
loop, and posts the sealed answer back. The blocking ``urllib`` calls run on
a **dedicated** executor — never the loop's default one, which the rest of
the daemon shares; parking eight held 25-second HTTPS requests there would
starve ``store_upload``, stats and every other ``run_in_executor`` user.

Freshness rules, both load-bearing:

- **The key is re-read from the store per frame** — the ``devices.resolve``
  discipline, no cached verdict — so ``relay.forget`` (un-pair) bites on the
  very next envelope and the channel goes silent.
- **Rate limits run before any decryption work** (frames per minute) and
  again before any execution (writes per minute), so a captured channel id
  buys denial of service bounded twice, and nothing else.

Unverifiable frames are dropped and counted; they never reach
``_remote_run``. The one refusal answered rather than dropped is a stale
counter — the answer is sealed to the phone and carries ``ctr_expected`` so a
phone that lost its counter state can fast-forward, which is safe because a
counter is worthless without the key.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from . import devices, identity, relay

logger = logging.getLogger("dark-army")

#: How long one long-poll request may hold. The mailbox answers within ~20s
#: (`maxDuration` 25); the connector re-makes the connection every half
#: minute even when nothing arrives.
POLL_TIMEOUT_SECONDS = 30.0
#: The pause after a failed poll — mailbox unreachable, TLS refusal, 5xx —
#: so a dead relay costs a retry every few seconds, not a spin.
RETRY_SECONDS = 5.0
#: The floor under an *empty* poll. A well-behaved mailbox holds the poll
#: open for ~20s before an empty answer; one that ignores the held-poll flag
#: returns instantly, and without a floor the loop is a tight HTTPS spin.
EMPTY_POLL_FLOOR_SECONDS = 1.0
#: How often the supervisor reconciles running tasks against the store's
#: channels, so a phone paired mid-run gets its task without a restart.
SUPERVISE_SECONDS = 5.0
#: While a channel is failing, at most one reminder line this often. The
#: transition lines are what say *what happened*; this is the heartbeat that
#: keeps a week-long outage visible without a line per five-second retry.
HEALTH_REPORT_SECONDS = 600.0
#: A channel is **active** (held long-polls) while the last verified frame
#: from the device (``relay.last_frame_at``, persisted) is inside this
#: window, or the last buzz the mailbox accepted for it
#: (``_push_health[...].last_ok_at``, memory only — a restart or a
#: ``remote_access`` toggle forgets it) is inside the shorter
#: ``PUSH_ARM_SECONDS``. Nothing periodic re-arms it. Each arming spends at most one
#: active window: ≈ 52 commands per 20-s held poll ≈ 156/min × 10 min ≈
#: 1,560 Upstash commands, once, only while a phone is genuinely in
#: play. Idle stays ≈ 6/min. Pushes are bounded upstream by
#: ``AlertPolicy`` and ``PUSH_MAX_PER_MINUTE``.
ACTIVE_WINDOW_SECONDS = 600.0
#: The push leg's own, shorter arming. A buzz the mailbox took used to hold
#: the full active window, so every alert bought ten minutes of held polls
#: whether or not anybody picked the phone up. The leg only has to bridge
#: from the buzz to the tap: the phone's first frame re-arms the full window
#: on its own, and a buzz nobody answers lapses in a minute and a half.
PUSH_ARM_SECONDS = 90.0
#: The socket relay's `peer:1` — the phone opened its line, which it does
#: the moment it wakes — arms the held polls for as long as a buzz does:
#: long enough to carry the wake's first requests if the socket cannot, and
#: the phone's first mailbox frame re-arms the full window by itself. A wake
#: that the socket serves costs at most this much held polling (≈ 234
#: Upstash commands), never the full window.
PEER_ARM_SECONDS = 90.0
#: Idle pacing. A non-wait GET costs the mailbox one RPOP (plus its two rate
#: commands) against the ~52 an empty 20-second held poll spends, and the gap
#: doubles from the first to the second until a frame or a buzz arms the
#: window. Worst-case pickup of the first away frame after a quiet spell is
#: ``IDLE_GAP_MAX``, well inside the mailbox's 120s TTL, and the first
#: carried frame (or accepted buzz) flips the channel active so everything
#: after it is fast.
IDLE_GAP_START = 5.0
IDLE_GAP_MAX = 30.0
#: Pushes per device per minute, taken before any network work. Alerts are
#: already transition-gated and cooled down upstream (`AlertPolicy`), so this
#: is a backstop against a policy bug turning into a buzzing pocket — well
#: under the push route's own 30/min cap so a healthy Mac never trips the
#: mailbox's 429.
PUSH_MAX_PER_MINUTE = 10
# The kind words a buzz may carry — `alerts.KINDS`, restated here so the wire
# check reads as a closed set in the file that writes the wire. Anything else
# is not sent: the mailbox then plays its default sound, exactly as before.
PUSH_KINDS = ("security", "permission", "question", "picks", "attention",
              "finished")
#: Cast slugs a buzz may name as its portrait. `identity.NAMES` lowercased,
#: every one ASCII (`test_identity.py` pins that). The live card's slug is
#: checked against the same tuple (`push_activity`).
#: A value outside the tuple is dropped, never a failed buzz.
PUSH_FACE_SLUGS = tuple(name.lower() for name in identity.NAMES)
#: The most a buzz's second line may be. A banner's subtitle shows about this
#: much before iOS truncates it, and both possible sources are far longer: a
#: card title runs to `board.MAX_TITLE_CHARS` (200) and a session's own name is
#: whatever a person typed. Clamped here, on the Mac, so nothing unbounded ever
#: leaves the machine; `relay/api/push.js` restates it as `MAX_WORK_CHARS` and
#: clamps again, and `test_phone_buzz_kinds.py` pins the two equal.
PUSH_WORK_CHARS = 80
#: The most a buzz's third line may be. The title and subtitle already take
#: two lines, and iOS shows a body of two to three more before it folds the
#: banner; both possible sources are far longer: a question runs to
#: `session_stats.MAX_QUESTION_CHARS` (600) and a `bob-tldr` summary is
#: whatever the agent wrote. Clamped here, on the Mac, so nothing unbounded
#: ever leaves the machine; `relay/api/push.js` restates it as
#: `MAX_NEED_CHARS` and clamps again, and `test_phone_buzz_kinds.py` pins
#: the two equal.
PUSH_NEED_CHARS = 120

#: What a buzz may offer a phone to press, and the identifier shape both
#: ids must fit (`push.js` pins the same regex). Opaque ids, never words.
#: `PUSH_ID_SHAPE` also shapes the buzz's *subject* — the `session_id` and
#: `card_id` a single-alert buzz carries on every device so the phone can
#: open the card or agent from the picture it holds; a value out of shape
#: is dropped silently (`work`'s rule), never a refusal.
PUSH_ACTS = ("permission", "acknowledge", "review")
PUSH_ID_SHAPE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
#: The live card's leg (`push_activity`): the kind words a card may wear —
#: `live_activity.KINDS`, restated here because this file writes the wire and
#: `push.js` pins the same array — the two ActivityKit events, and the face
#: slug's shape (a lowercase cast name, or empty). Never `title`: an
#: undeployed mailbox answers a body without one with 400, which is exactly
#: the refusal an old relay should give the new leg — never a mis-sent buzz.
ACTIVITY_KINDS = ("permission", "question", "picks", "attention")
ACTIVITY_EVENTS = ("update", "end")
#: What `push_activity_outcome` can answer; the daemon's retry rule reads it.
ACTIVITY_OUTCOMES = ("landed", "refused", "dead", "unreachable", "skipped")
#: The fleet card's seven extra fields, in wire order. Pinned equal to
#: `live_activity.COUNT_KEYS + FIGURE_KEYS` by `test_phone_buzz_kinds.py`.
ACTIVITY_FLEET_KEYS = ("working", "needs_you", "standing_by", "cost_usd", "tokens_k",
                       "cost_usd_hour", "tokens_k_hour")
#: A count or a token figure above this is clamped, not sent raw.
_FLEET_INT_MAX = 999999
#: The mailbox's own word for "this route is not wired up yet" — answered
#: by `push.js` when PUSH_SECRET or any APNs variable is unset. Paired with a
#: bare 404 (a mailbox deployed before `push.js` existed at all), it is the
#: not-yet-deployed case: quiet, said once per run, counted nowhere.
PUSH_ROUTE_MISSING_BODY = b"unconfigured"

_WRITE_LIMIT_REFUSAL = "too many remote requests — slow down"


def _fleet_int(value):
    """A fleet count or token figure, or None when it must not be sent.

    An int in ``0.._FLEET_INT_MAX`` is clamped at the top. A negative, a
    non-integer (a float, a string, a bool) is dropped — not sent as junk.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return min(value, _FLEET_INT_MAX)


def _fleet_usd(value):
    """A measured cost to the cent, or None when it must not be sent.

    A finite float ≥ 0, rounded to two places. Absent stays absent; a
    negative, a non-number or a non-finite value is dropped.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return round(number, 2)


def _url_ok(url: str) -> bool:
    """``https://`` only, with one stated exception: plain HTTP to loopback,
    which is the test seam (a fake mailbox on 127.0.0.1) and cannot leak on
    a network. ``relay.set_url`` already refuses storing anything else."""
    if url.startswith("https://"):
        return True
    return url.startswith("http://127.0.0.1") or url.startswith("http://localhost")


def _next_gap(now: float, last_frame_at: float, last_push_at: float,
              idle_gap: float,
              last_peer_at: float = 0.0) -> tuple[bool, float, float]:
    """(held, sleep_if_empty, next_idle_gap) — the connector's whole
    pacing decision, in one place. ``held`` says whether the next poll
    asks the mailbox to hold (the held-poll flag); ``sleep_if_empty`` is
    the pause after an empty answer (0.0 while held — the floor rule in
    ``_serve`` stays); ``next_idle_gap`` is the ladder position to carry
    forward. A stamp in the future (clock stepped back) reads as held —
    today's behaviour, bounded by the clock catching up.

    The two stamps arm different lengths: a verified frame holds the full
    ``ACTIVE_WINDOW_SECONDS``, an accepted buzz only ``PUSH_ARM_SECONDS``.
    The phone's first frame re-arms the full window by itself, so the push
    leg only needs to bridge until a tap lands. ``last_peer_at`` — the
    phone opening its socket (`relay.note_phone_arrived`) — arms
    ``PEER_ARM_SECONDS`` on the same terms."""
    since_frame = now - float(last_frame_at or 0.0)
    since_push = now - float(last_push_at or 0.0)
    since_peer = now - float(last_peer_at or 0.0)
    held = (since_frame <= ACTIVE_WINDOW_SECONDS
            or since_push <= PUSH_ARM_SECONDS
            or since_peer <= PEER_ARM_SECONDS)
    if held:
        return True, 0.0, IDLE_GAP_START
    return False, idle_gap, min(IDLE_GAP_MAX, idle_gap * 2.0)


class _Bucket:
    """A per-minute token bucket. Refilled continuously, capped at a minute's
    worth, consulted synchronously on the loop."""

    def __init__(self, per_minute: int):
        self._rate = per_minute / 60.0
        self._capacity = float(per_minute)
        self._tokens = float(per_minute)
        self._stamp = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self._capacity,
                           self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class _Health:
    """One channel's away standing, as the connector sees it.

    Lives on the loop and is read off it by ``health_snapshot`` — which is
    why that method copies scalars rather than handing this object out.
    """

    __slots__ = ("state", "status", "failures", "since", "last_ok_at",
                 "last_report")

    def __init__(self) -> None:
        self.state = "ok"
        #: The last HTTP status that failed, or ``0`` for a transport failure
        #: (no answer at all).
        self.status = 0
        self.failures = 0
        #: Monotonic, when the current failing run started. 0 while ok.
        self.since = 0.0
        #: Wall clock, when the mailbox last answered this channel at all.
        self.last_ok_at = 0.0
        #: Monotonic, when the last log line about this channel was written.
        self.last_report = 0.0



@dataclass(frozen=True)
class _Hops:
    """The three legs `_serve` and `_handle_wire` measure before a request
    runs (`link_timing.py`): mailbox dwell (two clocks, approximate), the
    held GET's wait and the seal's opening. `_execute` adds `run` and
    `answer` and files the record."""
    dwell: float = 0.0
    hold: float = 0.0
    open: float = 0.0


class RelayConnector:
    """Owns the channel tasks and the dedicated executor. Built once by the
    daemon, started and stopped on the loop by ``set_remote_access``."""

    def __init__(self, api):
        self._api = api
        self._tasks: dict[str, asyncio.Task] = {}
        #: In-flight request handlers — one per verified frame, so a slow
        #: action cannot block its channel's polling. Bounded by the buckets.
        self._handlers: set[asyncio.Task] = set()
        self._supervisor: asyncio.Task | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._frame_buckets: dict[str, _Bucket] = {}
        self._write_buckets: dict[str, _Bucket] = {}
        self._key_buckets: dict[str, _Bucket] = {}
        self._push_buckets: dict[str, _Bucket] = {}
        #: Per-device ordering locks for `_answer`: ctr allocation and the
        #: mailbox POST must be one step. Two concurrent `_execute` answers
        #: otherwise race between `next_send_ctr` and the POST, a late one
        #: lands first in the FIFO with the higher ctr, and the phone —
        #: whose `recvCtr` advances on the pop — refuses the earlier answer
        #: as a replay: the action ran, the phone reports failure.
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        #: Refused frames by refusal code — a count, never a body.
        self.dropped: dict[str, int] = {}
        self._warned_url = ""
        #: Per-device away standing. Loop-owned; see ``health_snapshot``.
        self._health: dict[str, _Health] = {}
        #: Per-device buzz standing. Loop-owned and **never snapshotted** —
        #: it exists precisely so a refused buzz cannot move ``relay_health``,
        #: which answers one question only: can the Mac reach the mailbox.
        self._push_health: dict[str, _Health] = {}
        #: Per-device live-card standing — the buzz's discipline, its own
        #: record, so a live-card refusal against an old mailbox neither
        #: flaps the buzz's record nor says "the phone will not be buzzed".
        #: Log-only, like the buzz's.
        self._activity_health: dict[str, _Health] = {}
        #: One informational line per connector run about a mailbox whose
        #: push route is not deployed yet. Not an outage, so not a failure.
        self._push_route_missing_said = False
        #: One line per run when a landed fleet update's reply lacks
        #: `shape=2` — the mailbox dropped the five fields. Not an outage.
        self._activity_shape_said = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """On the loop. Idempotent — a second enable is a no-op."""
        if self._running:
            return
        self._running = True
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=devices.MAX_DEVICES + 1,
                thread_name_prefix="bob-relay")
        self._supervisor = asyncio.get_running_loop().create_task(
            self._supervise())
        logger.info("relay connector started")

    async def stop(self) -> None:
        """On the loop. Cancels every task and abandons in-flight polls."""
        if not self._running and self._supervisor is None:
            return
        self._running = False
        pending = [t for t in (self._supervisor, *self._tasks.values(),
                               *self._handlers)
                   if t is not None]
        self._supervisor = None
        self._tasks = {}
        self._handlers = set()
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        # The transition guard is reset here on purpose: a connector that is
        # started again must be able to say "failing" afresh rather than
        # sitting on a stale verdict from the previous run and staying quiet.
        self._health = {}
        # Same reasoning for the buzz track, and for the once-per-run note: a
        # fresh connector may be facing a mailbox that has since been
        # redeployed, and must be able to say so again.
        self._push_health = {}
        self._activity_health = {}
        self._push_route_missing_said = False
        self._activity_shape_said = False
        logger.info("relay connector stopped")

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
            logger.warning("relay supervisor failed", exc_info=True)

    # ── one channel ───────────────────────────────────────────────────────────

    async def _serve(self, device_id: str) -> None:
        """One device's loop: poll, verify, execute, answer. Never dies
        silently — every failure is a log line and a retry pause.

        The poll has two speeds; ``_next_gap`` is the one decision,
        held for ``ACTIVE_WINDOW_SECONDS`` after a verified frame
        (``relay.last_frame_at``, persisted) and for the shorter
        ``PUSH_ARM_SECONDS`` after a mailbox-accepted buzz
        (``_last_push_ok_at``, memory only so a restart or a
        ``remote_access`` toggle forgets it — a buzz is stale in
        seconds and the phone's first frame re-arms the window).
        Nothing periodic re-arms it; each arming costs at most one
        active window (≈ 1,560 Upstash commands).
        """
        idle_gap = IDLE_GAP_START
        try:
            while self._running:
                key = relay.channel_key(device_id)
                if key is None:
                    return  # forgotten mid-run: the channel goes silent
                url = relay.get_url()
                if not url or not _url_ok(url):
                    if url and url != self._warned_url:
                        self._warned_url = url
                        logger.warning(
                            "relay address refused: must start with https://")
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                chan = relay.channel_id(key)
                # Cleared before the stamps are read, so a `peer:1` landing
                # while this poll is out still cuts the idle sleep after it.
                arrival = relay.phone_arrival_waiter(device_id)
                arrival.clear()
                held, gap, next_gap = _next_gap(
                    time.time(), relay.last_frame_at(device_id),
                    self._last_push_ok_at(device_id), idle_gap,
                    relay.last_phone_arrived_at(device_id))
                poll = f"{url}/api/box?ch={chan}&dir=to-mac"
                if held:
                    poll += "&wait=1"
                started = time.monotonic()
                try:
                    status, body = await self._http("GET", poll)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._note_failure(device_id, 0)
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                if status in (200, 204):
                    self._note_ok(device_id)
                else:
                    self._note_failure(device_id, status)
                if status != 200 or not body:
                    if status not in (200, 204):
                        await asyncio.sleep(RETRY_SECONDS)
                    elif not held:
                        # The idle gap, cut short by the phone waking.
                        try:
                            await asyncio.wait_for(arrival.wait(), gap)
                        except asyncio.TimeoutError:
                            pass
                        idle_gap = next_gap
                    elif time.monotonic() - started < EMPTY_POLL_FLOOR_SECONDS:
                        # An instant empty answer means the mailbox ignored
                        # the held poll — floor the loop rather than spin.
                        await asyncio.sleep(EMPTY_POLL_FLOOR_SECONDS)
                        idle_gap = next_gap
                    else:
                        idle_gap = next_gap
                    continue
                idle_gap = IDLE_GAP_START
                # The link timing's first two hops (`link_timing.py`):
                # the wall clock at pickup, which the phone's own `ts`
                # inside the frame is subtracted from for the mailbox
                # dwell, and how long this held GET waited (monotonic).
                picked = time.time()
                hold = time.monotonic() - started
                try:
                    await self._handle_wire(device_id, body, picked=picked,
                                            hold=hold)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A malformed mailbox body must never kill the channel.
                    logger.warning("relay frame handling failed",
                                   exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay channel task failed", exc_info=True)

    async def _handle_wire(self, device_id: str, body: bytes, *,
                           picked: Optional[float] = None,
                           hold: float = 0.0) -> None:
        """One mailbox payload: buckets, seal and counters here, **inline**,
        so counters are noted in arrival order and the durable
        persist-before-execute rule holds; the run and its answer are spawned
        as their own task, so one slow action (`prepare_card` holds the
        phone's 135s deadline) cannot block this channel's polling and turn
        the screen unreachable mid-prepare.

        ``picked`` (wall clock at pickup) and ``hold`` (how long the GET
        waited) come from `_serve`; the seal's opening is timed here and
        the three ride to `_execute` as `_Hops`, which finishes the record.
        """
        if not self._frame_bucket(device_id).take():
            self._drop("rate", device_id)
            return
        # Key re-read *here*, per frame, not once per task: `forget` must
        # bite between two frames of one running loop.
        key = relay.channel_key(device_id)
        if key is None:
            return
        try:
            wire = body.decode("ascii").strip()
        except UnicodeDecodeError:
            self._drop("shape", device_id)
            return
        last = relay.last_recv_ctr(device_id)
        opened_at = time.monotonic()
        frame, err = relay.open_frame(key, relay.DIR_PHONE_TO_MAC, wire, last)
        open_seconds = time.monotonic() - opened_at
        if err == "ctr":
            # The one refusal worth answering: sealed to the phone, so a
            # counter is handed only to the other keyholder.
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
        relay.note_recv_ctr(device_id, int(frame["ctr"]),
                            durable=(kind == "action"))
        payload = frame.get("body")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "action" and not self._write_bucket(
                device_id, relay.write_bucket_kind(payload)).take():
            await self._answer(device_id, key, {
                "re": str(frame.get("id") or ""), "status": 429,
                "error": _WRITE_LIMIT_REFUSAL,
            }, kind="err")
            return
        # Mailbox dwell: Mac wall clock at pickup minus the phone's own
        # stamp inside the opened frame. Two clocks, so approximate —
        # `RELAY_SKEW_SECONDS` bounds the disagreement — clamped at zero
        # and never corrected; `hold` beside it is the one-clock bound.
        try:
            stamped = float(frame.get("ts") or 0.0)
        except (TypeError, ValueError):
            stamped = 0.0
        pickup = float(picked) if picked is not None else time.time()
        hops = _Hops(dwell=max(0.0, pickup - stamped) if stamped > 0 else 0.0,
                     hold=max(0.0, float(hold or 0.0)),
                     open=max(0.0, open_seconds))
        task = asyncio.get_running_loop().create_task(
            self._execute(device_id, key, kind, payload,
                          str(frame.get("id") or ""), hops))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _execute(self, device_id: str, key: bytes, kind: str,
                       payload: dict, frame_id: str,
                       hops: Optional["_Hops"] = None) -> None:
        """One verified request's run and sealed answer — its own task, off
        the channel's polling loop. Never dies silently.

        The reply envelope carries ``timing`` beside ``body`` — the Mac's
        `dwell` / `hold` / `open` / `run` legs, so the phone can draw the
        whole trip — never inside the state body, so `_state_digest` is
        untouched and an unchanged poll stays short. After the answer has
        gone the record goes to `ApiServer.note_link_timing`, `_drop`'s
        way: under `try`/`except`, because a test double standing in for
        the server may not carry it, and a record must never fail a reply.
        """
        hops = hops or _Hops()
        try:
            run_at = time.monotonic()
            status, ctype, out = await self._api._remote_run(
                kind, payload, device_id)
            run_seconds = time.monotonic() - run_at
            answer_at = time.monotonic()
            sent = await self._answer(device_id, key, {
                "re": frame_id,
                "status": int(status),
                "content_type": str(ctype),
                "body": out.decode("utf-8", "replace"),
                "timing": {"dwell": hops.dwell, "hold": hops.hold,
                           "open": hops.open, "run": run_seconds},
            }, kind="reply")
            answer_seconds = time.monotonic() - answer_at
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("relay request handling failed", exc_info=True)
            return
        try:
            self._api.note_link_timing("relay", device_id, kind, {
                "dwell": hops.dwell, "hold": hops.hold, "open": hops.open,
                "run": run_seconds, "answer": answer_seconds,
            }, int(status) if sent else 0, len(out))
        except Exception:
            logger.debug("link timing record skipped for relay %s", kind,
                         exc_info=True)

    async def _answer(self, device_id: str, key: bytes, body: dict,
                      *, kind: str) -> bool:
        """Seal and POST one Mac-to-phone frame. True when the mailbox took
        it (200 / 204); False when nothing was sent or the POST failed —
        the one reader is `_execute`'s timing record, which files a failed
        answer under status 0."""
        url = relay.get_url()
        if not url or not _url_ok(url):
            return False
        # Allocation and POST under one lock, per device: the phone's FIFO
        # mailbox pops in arrival order and its `recvCtr` only goes up, so
        # a higher ctr landing first turns the lower one into a "replay".
        async with self._send_lock(device_id):
            ctr = relay.next_send_ctr(device_id)
            if ctr <= 0:
                return False
            wire = relay.seal_frame(key, relay.DIR_MAC_TO_PHONE, ctr, kind,
                                    body)
            if not wire and kind == "reply":
                # The answer outgrew the frame cap — the phone would refuse
                # the oversize envelope unopened on every poll, so say so in
                # a small err it can open instead. A fresh counter: the
                # oversize wire was never sent, but reuse is not worth
                # reasoning about.
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
            chan = relay.channel_id(key)
            post = f"{url}/api/box?ch={chan}&dir=to-phone"
            try:
                status, _ = await self._http("POST", post,
                                             wire.encode("ascii"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self._note_failure(device_id, 0)
                return False
            else:
                if status in (200, 204):
                    self._note_ok(device_id)
                    return True
                self._note_failure(device_id, status)
                return False

    # ── push ──────────────────────────────────────────────────────────────────

    async def push_alert(self, device_id: str, body: dict) -> bool:
        """One nudge to one phone, through the mailbox's push route.

        ``body`` is the daemon's composed ``{title, badge, kind, work, need, face}``
        — ``work`` being the one short line naming what the agent is working
        on (the bound board card's title, else the session's own title),
        composed by `BobDaemon._compose_push_work`, and ``need`` the one line
        saying what is needed (the agent's own summary or question, or a
        tool's bare name), composed by `BobDaemon._compose_push_need`; each is
        **absent** when there is nothing to say. Both are clamped here
        (`PUSH_WORK_CHARS`, `PUSH_NEED_CHARS`) as well as there, because this
        file is the one place a field can join the wire. The token and
        its APNs environment are **re-read from the store here, at the moment
        of use** (the `channel_key` discipline), so ``relay.forget`` (un-pair)
        and an empty-token unregister both bite on the very next buzz. The
        blocking POST rides the dedicated executor via `_http`, never the
        loop's shared default one. ``False`` says only "nothing was sent";
        the caller has nothing to retry — a missed buzz is stale in seconds.
        """
        did = str(device_id or "")
        key = relay.channel_key(did)
        token = relay.push_token(did)
        if key is None or token is None:
            return False
        url = relay.get_url()
        if not url or not _url_ok(url):
            return False
        if not self._push_bucket(did).take():
            return False
        fields = {
            "tok": token,
            "env": relay.push_env(did),
            "title": str((body or {}).get("title") or ""),
            "badge": int((body or {}).get("badge") or 0),
        }
        kind = str((body or {}).get("kind") or "")
        if kind in PUSH_KINDS:
            fields["kind"] = kind
        work = " ".join(str((body or {}).get("work") or "").split())[:PUSH_WORK_CHARS]
        if work:
            fields["work"] = work
        need = " ".join(str((body or {}).get("need") or "").split())[:PUSH_NEED_CHARS]
        if need:
            fields["need"] = need
        # The portrait: a cast slug, joined only when it is one of the
        # roster. Anything else is dropped — a bad face must not fail the
        # buzz, and no image rides this field.
        face = str((body or {}).get("face") or "")
        if face in PUSH_FACE_SLUGS:
            fields["face"] = face
        # The subject: the opaque session id and card id a single-alert buzz
        # is about (`BobDaemon._compose_push_subject`), each joined only in
        # `PUSH_ID_SHAPE`. An empty or malformed value is dropped silently —
        # `work`'s rule, never a refusal — and a body with neither has
        # neither, byte-identical to before.
        for subject_key in ("session_id", "card_id"):
            value = str((body or {}).get(subject_key) or "")
            if PUSH_ID_SHAPE.match(value):
                fields[subject_key] = value
        # The lock-screen leg: an action word and identifiers, joined only
        # when the daemon composed them for this device (the desk's
        # per-phone switch) and only in the shapes `push.js` re-checks. The
        # act rides only on the shaped `session_id` already in `fields` — it
        # does not re-assign it — and a permission also needs a shaped rid.
        act = str((body or {}).get("act") or "")
        if act == "review":
            # A foreground button naming a review run: no session needed, and
            # `run_id` joins the alert leg only with this act.
            run = str((body or {}).get("run_id") or "")
            if PUSH_ID_SHAPE.match(run):
                fields["act"] = "review"
                fields["run_id"] = run
        elif act in PUSH_ACTS and "session_id" in fields:
            rid = str((body or {}).get("request_id") or "")
            if act != "permission" or PUSH_ID_SHAPE.match(rid):
                fields["act"] = act
                if act == "permission":
                    fields["request_id"] = rid
        from .decision_store import uuid_ok
        if type((body or {}).get("destination_version")) is int and body["destination_version"] == 1 and uuid_ok(body.get("receipt_id")):
            fields.update(destination_version=1, receipt_id=body["receipt_id"])
        payload = json.dumps(fields).encode("utf-8")
        post = f"{url}/api/push?ch={relay.channel_id(key)}"
        # The push route is the one the mailbox authenticates: `ch` is
        # caller-chosen, so without this header anyone holding the relay URL
        # could buzz any device token. The secret itself never appears in a
        # snapshot or a log line.
        headers = {}
        secret = relay.get_push_secret()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            status, reply = await self._http("POST", post, payload,
                                             headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._note_push_failure(did, 0)
            return False
        if status in (200, 204):
            self._note_push_ok(did)
            return True
        if self._push_route_missing(status, reply):
            # A mailbox deployed before `push.js` existed, or one whose
            # PUSH_SECRET / APNs variables are unset. Neither is an outage —
            # nothing is broken that a redeploy would not fix — so it is said
            # once per run and counted nowhere. The `_push_bucket` take above
            # still caps what a not-yet-deployed mailbox costs at ten
            # requests a minute per device.
            if not self._push_route_missing_said:
                self._push_route_missing_said = True
                logger.info(
                    "the mailbox has no push route yet (status %s) — "
                    "redeploy the relay folder (vercel deploy --prod) and set "
                    "its PUSH_SECRET and APNs variables before the phone can "
                    "be buzzed", status)
            return False
        if status == 401:
            # One line for the event, not two: the wording *is* the buzz
            # track's transition line. It names what to fix; it never names
            # the secret itself.
            self._note_push_failure(
                did, 401,
                transition_line="the relay refused the push secret — set the "
                                "mailbox's PUSH_SECRET in the relay sheet "
                                "before the phone can be buzzed")
            return False
        self._note_push_failure(did, status)
        return False

    async def push_activity(self, device_id: str, body: dict) -> bool:
        """`push_activity_outcome`, as the one bit the plan named: did the
        mailbox take it."""
        return await self.push_activity_outcome(device_id, body) == "landed"

    async def push_activity_outcome(self, device_id: str, body: dict) -> str:
        """One Live Activity update or end to one phone — `push_alert`'s twin.

        ``body`` is ``{"event", "nickname", "slug", "kind", "work", "since",
        "session_id"}`` as `BobDaemon._push_live_activity` composed it
        (`live_activity.content_state` plus the event). Joined to the wire
        **only** in shape: ``event`` from `ACTIVITY_EVENTS`, ``kind`` from
        `ACTIVITY_KINDS` (an update with any other word sends nothing; an
        end carries the last state and may have none), ``slug`` by
        `PUSH_FACE_SLUGS` (else empty), ``work`` clamped to `PUSH_WORK_CHARS`,
        ``since`` a finite number ≥ 0, ``session_id`` by `PUSH_ID_SHAPE` (a
        bad id is dropped, never a reason to keep a stale card up). Never
        ``title``, so a pre-change `push.js` answers 400 — routed to the
        buzz track as a failure, never to `_push_route_missing`, and never
        re-shaped into an alert (an ActivityKit token pushed as an alert is
        Apple's BadDeviceToken, harmless but noisy).

        The activity token and its env are re-read from the store at the
        moment of use (`push_token`'s discipline); the `_push_bucket` is
        shared with the buzz, so the two legs together stay under
        `PUSH_MAX_PER_MINUTE`.

        The answer is one of `ACTIVITY_OUTCOMES`, because the caller's retry
        rule turns on *why* it did not land: ``"landed"`` (the mailbox took
        it — remember the state); ``"refused"`` (a 4xx, Apple's refusal, an
        undeployed route: the same body will be refused again, so it is not
        resent until the picture changes); ``"dead"`` (the mailbox's 502:
        Apple refused the activity's token, most often because iOS ended
        the activity at its eight-hour limit — no body will land on that
        token again, so nothing is sent until the phone registers a new
        one); ``"unreachable"`` (no answer, a
        5xx: retry, but no sooner than `daemon.LIVE_ACTIVITY_RETRY_SECONDS`);
        ``"skipped"`` (no token any more, a shape this method would not
        send, the bucket empty — nothing left the Mac and nothing is
        remembered).
        """
        did = str(device_id or "")
        key = relay.channel_key(did)
        token = relay.activity_token(did)
        if key is None or token is None:
            return "skipped"
        url = relay.get_url()
        if not url or not _url_ok(url):
            return "skipped"
        body = body or {}
        event = str(body.get("event") or "")
        if event not in ACTIVITY_EVENTS:
            return "skipped"
        kind = str(body.get("kind") or "")
        if kind not in ACTIVITY_KINDS:
            if event == "update":
                return "skipped"
            kind = ""
        slug = str(body.get("slug") or "")
        if slug not in PUSH_FACE_SLUGS:
            slug = ""
        try:
            since = float(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0.0
        if since != since or since in (float("inf"), float("-inf")) or since < 0:
            since = 0.0
        if not self._push_bucket(did).take():
            return "skipped"
        fields = {
            "tok": token,
            "env": relay.activity_env(did),
            "event": event,
            "nickname": " ".join(str(body.get("nickname") or "").split())[:40],
            "slug": slug,
            "work": " ".join(str(body.get("work") or "").split())[:PUSH_WORK_CHARS],
            "since": since,
        }
        if kind:
            fields["kind"] = kind
        sid = str(body.get("session_id") or "")
        if PUSH_ID_SHAPE.match(sid):
            fields["session_id"] = sid
        # A review run's id (`picks`): joined by the same shape, and only
        # when there is one — every other card's wire is unchanged.
        run_id = str(body.get("run_id") or "")
        if PUSH_ID_SHAPE.match(run_id):
            fields["run_id"] = run_id
        for field in ("working", "needs_you", "standing_by", "tokens_k",
                      "tokens_k_hour"):
            if field not in body:
                continue
            number = _fleet_int(body.get(field))
            if number is None:
                continue
            fields[field] = number
        for field in ("cost_usd", "cost_usd_hour"):
            if field not in body:
                continue
            cost = _fleet_usd(body.get(field))
            if cost is not None:
                fields[field] = cost
        carried_fleet = any(key in fields for key in ACTIVITY_FLEET_KEYS)
        payload = json.dumps(fields).encode("utf-8")
        post = f"{url}/api/push?ch={relay.channel_id(key)}"
        headers = {}
        secret = relay.get_push_secret()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            status, reply = await self._http("POST", post, payload,
                                             headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._note_activity_failure(did, 0)
            return "unreachable"
        if status in (200, 204):
            self._note_activity_ok(did)
            # A mailbox that learned the five fields answers `ok shape=2`.
            # One that did not drops them and answers `ok`; say so once,
            # and only when this body actually carried a fleet field.
            if carried_fleet and b"shape=2" not in (reply or b""):
                if not self._activity_shape_said:
                    self._activity_shape_said = True
                    logger.info(
                        "the mailbox drops the fleet figures — redeploy the "
                        "relay folder (vercel deploy --prod)")
            return "landed"
        if self._push_route_missing(status, reply):
            if not self._push_route_missing_said:
                self._push_route_missing_said = True
                logger.info(
                    "the mailbox has no push route yet (status %s) — "
                    "redeploy the relay folder (vercel deploy --prod) and set "
                    "its PUSH_SECRET and APNs variables before the phone can "
                    "be buzzed", status)
            return "refused"
        if status == 401:
            self._note_activity_failure(
                did, 401,
                transition_line="the relay refused the push secret — set the "
                                "mailbox's PUSH_SECRET in the relay sheet "
                                "before the phone's live card can be updated")
            return "refused"
        # A 400 here is the old mailbox refusing a body with no `title`, a
        # 502 is Apple refusing the token (an ended activity's, most often;
        # `push.js` answers 502 only for Apple's 4xx on this leg and maps
        # Apple's 5xx and 429 to `503 unavailable`, which is unreachable
        # below): a live-card failure in words, never an outage of the away
        # link, and not one that the same body sent again would clear.
        self._note_activity_failure(did, status)
        if status == 502:
            return "dead"
        if 400 <= status < 500:
            return "refused"
        return "unreachable"

    @staticmethod
    def _push_route_missing(status: int, body: bytes) -> bool:
        """Is this the mailbox saying "no push route here yet"? A 404 is
        Vercel's own answer for a route that does not exist — its body is
        HTML and is not inspected. A 503 is only this case when the body is
        the route's own ``unconfigured``; ``unavailable`` (Upstash or Apple
        down) is a real failure and goes to the buzz track."""
        return status == 404 or (
            status == 503
            and (body or b"").strip().lower() == PUSH_ROUTE_MISSING_BODY)

    # ── health ────────────────────────────────────────────────────────────────
    #
    # Three sites feed the **channel** record: the poll's transport failure,
    # the poll's non-200/204 answer (which used to be entirely silent — a 429
    # or 503 was retried every five seconds forever with nothing said at any
    # level), and the answer POST. What comes out is **transitions plus a
    # heartbeat**, never a line per retry: a permanently dead mailbox costs
    # one warning, then one reminder per `HEALTH_REPORT_SECONDS`.
    #
    # The buzz has **its own record** below (`_push_health`), on exactly the
    # same discipline but logged and never published. A refused push says
    # nothing about whether the Mac can reach the mailbox, and feeding one
    # into `_health` hid real outages behind a push route that is merely not
    # deployed yet.

    @staticmethod
    def _record_ok(health: _Health, *, subject: str) -> None:
        """One record's bookkeeping for "it worked". The wording is the
        caller's; the discipline is shared."""
        health.last_ok_at = time.time()
        if health.state != "ok":
            logger.info("%s recovered after %d failures (last status %s)",
                        subject, health.failures, health.status)
        health.state = "ok"
        health.status = 0
        health.failures = 0
        health.since = 0.0
        health.last_report = 0.0

    @staticmethod
    def _record_failure(health: _Health, status: int, *, subject: str,
                        consequence: str,
                        transition_line: str | None = None) -> None:
        """One record's bookkeeping for "it did not". ``transition_line``
        **replaces** the composed ok→failing line — it does not precede it,
        which would be two lines for one event; the reminder stays generic."""
        now = time.monotonic()
        health.status = int(status)
        health.failures += 1
        if health.state == "ok":
            health.state = "failing"
            health.since = now
            health.last_report = now
            if transition_line is not None:
                logger.warning("%s", transition_line)
            else:
                logger.warning("%s failing (status %s) — %s",
                               subject, status or "no answer", consequence)
            return
        if now - health.last_report >= HEALTH_REPORT_SECONDS:
            health.last_report = now
            logger.warning("%s still failing (status %s) after %d failures",
                           subject, status or "no answer", health.failures)

    def _health_for(self, device_id: str) -> _Health:
        health = self._health.get(device_id)
        if health is None:
            health = _Health()
            self._health[device_id] = health
        return health

    def _note_ok(self, device_id: str) -> None:
        """The mailbox answered. A 204 counts — an empty box is a healthy
        one; what is being tracked is whether the away path *works*."""
        self._record_ok(self._health_for(device_id), subject="relay channel")

    def _note_failure(self, device_id: str, status: int) -> None:
        """The mailbox refused or could not be reached. ``status`` is the
        HTTP code, or ``0`` when there was no answer at all."""
        self._record_failure(
            self._health_for(device_id), status, subject="relay channel",
            consequence="the away path is down until this clears")

    # The buzz track. Same helpers, its own dict, and deliberately no reader
    # outside this file: `health_snapshot` below touches `_health` alone.

    def _push_health_for(self, device_id: str) -> _Health:
        health = self._push_health.get(device_id)
        if health is None:
            health = _Health()
            self._push_health[device_id] = health
        return health

    def _last_push_ok_at(self, device_id: str) -> float:
        """When the mailbox last took a buzz for this device, wall clock;
        0.0 if never this run. Reads without creating a record."""
        health = self._push_health.get(device_id)
        return float(health.last_ok_at) if health is not None else 0.0

    def _note_push_ok(self, device_id: str) -> None:
        """The mailbox took the buzz."""
        self._record_ok(self._push_health_for(device_id), subject="phone buzz")

    def _note_push_failure(self, device_id: str, status: int, *,
                           transition_line: str | None = None) -> None:
        """The buzz was refused or went nowhere. Never touches ``_health``,
        and never the live card's record: the two leave through one route
        but fail for different reasons (an old mailbox takes the buzz and
        refuses the card), and one record for both flapped a warning pair
        per buzz and said "will not be buzzed" about a card."""
        self._record_failure(
            self._push_health_for(device_id), status, subject="phone buzz",
            consequence="the phone will not be buzzed until this clears",
            transition_line=transition_line)

    # The live card's track. The buzz's discipline, its own record, its own
    # consequence in words; log-only like the buzz's.

    def _activity_health_for(self, device_id: str) -> _Health:
        health = self._activity_health.get(device_id)
        if health is None:
            health = _Health()
            self._activity_health[device_id] = health
        return health

    def _note_activity_ok(self, device_id: str) -> None:
        """The mailbox took the live card's update or end."""
        self._record_ok(self._activity_health_for(device_id),
                        subject="phone live card")

    def _note_activity_failure(self, device_id: str, status: int, *,
                               transition_line: str | None = None) -> None:
        """The live card's update was refused or went nowhere. Never
        touches ``_health`` or ``_push_health``."""
        self._record_failure(
            self._activity_health_for(device_id), status,
            subject="phone live card",
            consequence="the phone's live card will not be updated until "
                        "this clears",
            transition_line=transition_line)

    def health_snapshot(self) -> dict:
        """The away link's standing, for ``devices_snapshot``. **Statuses and
        clock readings only** — never a key, a digest or a channel id, which
        is derived from the key and must not travel on an ungated `/api/state`.

        Called off the loop, so every field is a scalar read in one pass; the
        live ``_Health`` is never handed out. A worst-first pick: any failing
        channel is what the person needs told.
        """
        # `_health` alone, on purpose: the buzz track is log-only.
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
                # Age in seconds rather than a monotonic reading, which means
                # nothing outside this process.
                "failing_for": max(0.0, time.monotonic() - worst.since),
                "last_ok_at": float(worst.last_ok_at),
            }
        best = max(records, key=lambda h: h.last_ok_at)
        return {
            "state": "ok",
            "status": 0,
            "failures": 0,
            "failing_for": 0.0,
            "last_ok_at": float(best.last_ok_at),
        }

    # ── plumbing ──────────────────────────────────────────────────────────────

    async def _http(self, method: str, url: str,
                    data: bytes | None = None,
                    headers: dict | None = None) -> tuple[int, bytes]:
        """One blocking request on the dedicated executor. ``(status, body)``.
        4xx/5xx come back as a status, never an exception."""
        executor = self._executor
        if executor is None:
            raise RuntimeError("connector is stopped")

        def call() -> tuple[int, bytes]:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "text/plain")
            for name, value in (headers or {}).items():
                req.add_header(name, value)
            try:
                with urllib.request.urlopen(
                        req, timeout=POLL_TIMEOUT_SECONDS) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        return await asyncio.get_running_loop().run_in_executor(executor, call)

    def _frame_bucket(self, device_id: str) -> _Bucket:
        bucket = self._frame_buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(relay.RELAY_MAX_FRAMES_PER_MINUTE)
            self._frame_buckets[device_id] = bucket
        return bucket

    def _send_lock(self, device_id: str) -> asyncio.Lock:
        lock = self._send_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[device_id] = lock
        return lock

    def _write_bucket(self, device_id: str, kind: str = "writes") -> _Bucket:
        """The device's write bucket for this kind of action: raw terminal
        keys draw on their own (`relay.write_bucket_kind`), so typing from
        away never spends the allowance an answer needs."""
        keys = kind == "keys"
        buckets = self._key_buckets if keys else self._write_buckets
        bucket = buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(relay.RELAY_MAX_KEY_WRITES_PER_MINUTE if keys
                             else relay.RELAY_MAX_WRITES_PER_MINUTE)
            buckets[device_id] = bucket
        return bucket

    def _push_bucket(self, device_id: str) -> _Bucket:
        bucket = self._push_buckets.get(device_id)
        if bucket is None:
            bucket = _Bucket(PUSH_MAX_PER_MINUTE)
            self._push_buckets[device_id] = bucket
        return bucket

    def _drop(self, code: str, device_id: str = "") -> None:
        """Count one refused mailbox payload, and write it to the access
        log. The peer is the paired **device id** whose mailbox the payload
        landed in — the internet gives no sender address, and the channel
        id and mailbox URL are never logged. Under `try/except` because a
        test double standing in for the server may not carry the recorder,
        and a refusal must never stop the channel polling."""
        self.dropped[code] = self.dropped.get(code, 0) + 1
        try:
            self._api._record_access("relay", device_id, code,
                                     device_id=device_id)
        except Exception:
            logger.debug("access record skipped for relay %s", code,
                         exc_info=True)
