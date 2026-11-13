"""The steering queue's pure half: the rules and the lanes, no store and no
daemon (`docs/steering-queue.md`).

Everything Dark Army types into a session waits in that session's *lane*, one
sender at a time, in the order it arrived. `Lanes` is the ordering; it does
no I/O of its own. What a lane's worker does with an entry is a callable the
daemon hands in (`daemon_steering.SteeringMixin._steer_execute`), and the
table of what is written down is `board_steering_store.py`'s.

Loop-only: every method here runs on the daemon's asyncio loop, and a lane's
worker is a task on that loop, so each lane has exactly one writer at a time.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from . import command_receipts

logger = logging.getLogger("dark-army.steering")

#: A queued message is replayed after a restart only while it is this young.
STEER_REPLAY_FRESH_SECONDS = 120
#: After a restart, replays wait this long so the sessions, the pty attach and
#: the editor windows are found again.
STEER_REPLAY_SETTLE_SECONDS = 10
#: How long a caller waits for its own entry before being told it is queued.
STEER_REPLY_WAIT_SECONDS = 20
#: Entries a lane holds at once (the running one included).
STEER_MAX_PENDING = 8
#: Raw keys waiting behind a busy lane, before the next frame is turned away.
STEER_MAX_RAW_PENDING = 256
#: The running entry's grace at shutdown, before it is cancelled.
STEER_SHUTDOWN_GRACE_SECONDS = 1.0
#: Resolved rows kept, oldest pruned first. A queued row is never pruned.
MAX_STEER_ROWS = 2000
#: How long a resolved row answers a phone's resend; the phone mark's own
#: lifetime, imported so the two cannot drift.
STEER_RETENTION_SECONDS = command_receipts.RECEIPT_SECONDS

STEER_QUEUED_NOTE = ("Queued behind earlier input to this session — it will "
                     "be typed in order.")
STEER_FULL_REFUSAL = ("Too much is already waiting to be typed into that "
                      "session — wait a moment and try again.")
STEER_INTERRUPTED_REFUSAL = ("Dark Army restarted while typing this — check "
                             "the terminal before sending it again.")
STEER_FAILED_REFUSAL = ("Dark Army could not type that — check the terminal "
                        "before sending it again.")
STEER_SHUTDOWN_REFUSAL = "Dark Army is shutting down — nothing was typed."
STEER_RESTARTING_NOTE = (
    "Dark Army is restarting — this will be typed after it comes back if it "
    "is still fresh, or reported as not delivered.")
STEER_NOT_DELIVERED_WHY = (
    "Dark Army restarted before typing it, and it was too old to type late.")
STEER_STALE_WHY = "it waited too long to be typed"
STEER_CARD_NOT_DELIVERED_NOTE = (
    "Dark Army restarted before this message was typed, so it was not "
    "delivered: “{excerpt}”. Send it again if you still want it.")
STEER_CARD_INTERRUPTED_NOTE = (
    "Dark Army restarted while typing a message to this card's session. It "
    "may have only partly arrived — check the terminal before sending it "
    "again.")


@dataclass(frozen=True)
class Kind:
    """What one kind of typed input is.

    `durable`: written down before it is typed. `replay`: typed again after a
    restart when it never started and is still fresh. `journal`: `"steer"`
    writes the `steer`/`type` intent itself, `"own"` leaves it to the verb's
    own action (the answer burst), `""` writes none."""
    durable: bool
    replay: bool
    journal: str


KINDS: dict = {
    "reply": Kind(True, True, "steer"),
    "card_message": Kind(True, True, "steer"),
    "card_ask": Kind(True, True, "steer"),
    "terminal_line": Kind(True, True, "steer"),
    "wrap_up": Kind(True, True, "steer"),
    "low_priority": Kind(True, True, "steer"),
    "answer": Kind(True, False, "own"),
    "autocompact": Kind(False, False, "own"),
    "limit_resume": Kind(False, False, "own"),
    "permission_key": Kind(False, False, ""),
    "raw": Kind(False, False, ""),
}

#: The diary's noun per kind.
WORDS: dict = {
    "reply": "a message",
    "card_message": "a card message",
    "card_ask": "a question to the card's session",
    "terminal_line": "a line",
    "wrap_up": "Wrap up's clear",
    "low_priority": "the low-priority switch",
    "answer": "an answer",
    "autocompact": "an auto-compact",
    "limit_resume": "a continue after the usage limit",
    "permission_key": "a permission key",
    "raw": "keys",
}

#: Every function in `daemon.py`, `daemon_board.py` and `daemon_steering.py`
#: that puts keystrokes or words on a session's input. `test_steering_routes`
#: fails by name when one appears that is not here, and when a caller of one
#: of these is neither here nor the lane's own runner.
TYPING_SITES = frozenset({
    "_reply_to_session_now",
    "_reply_native_codex",
    "_reply_shared_codex",
    "_reply_by_typing",
    "_wrap_up_session_now",
    "_low_priority_session_now",
    "_type_answer_burst",
    "_grok_permission_key_now",
    "_auto_compact_now",
    "_limit_resume_now",
    "_terminal_raw_input",
    "_terminal_line_input",
    "_message_card_now",
    "_ask_card_push_now",
})

#: Bodies the lane runs that reach a typing site without containing the call.
LANE_BODIES = frozenset({
    "_answer_question_now",
    "_answer_questions_now",
    "_terminal_line_now",
})


@dataclass(frozen=True)
class Caller:
    """Who is pressing: `"mac"` (the loopback door) or `"phone"` (a sealed
    door), and for a phone press the digest of its one-time mark."""
    source: str = "mac"
    dedupe: str = ""


CALLER: contextvars.ContextVar = contextvars.ContextVar(
    "dark_army_steering_caller", default=Caller("mac", ""))


@contextlib.contextmanager
def calling(caller: Caller):
    """Tag every steering verb awaited inside the block with `caller`."""
    token = CALLER.set(caller)
    try:
        yield caller
    finally:
        CALLER.reset(token)


def dedupe_digest(device_id, token) -> str:
    """The first 32 hex of SHA-256 over `device_id + NUL + token`, or `""`
    when either part is empty. The token itself is never kept."""
    device = str(device_id or "")
    mark = str(token or "")
    if not device or not mark:
        return ""
    return hashlib.sha256((device + "\0" + mark).encode("utf-8")).hexdigest()[:32]


def is_fresh(created_at, now) -> bool:
    """`0 <= now - created_at <= STEER_REPLAY_FRESH_SECONDS`. A negative age
    (a clock that went backwards) is stale: the failure direction is "not
    typed", never "typed late"."""
    try:
        age = float(now) - float(created_at)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= STEER_REPLAY_FRESH_SECONDS


def replay_verdict(row: dict, journal_state: str, now) -> str:
    """What a restart does with one `queued` row: `replay`, `not_delivered`,
    `interrupted`, `finished` or `own`.

    `journal_state` is `none`, `open`, `interrupted` or `finished:<outcome>`
    for a row whose `steer`/`type` intent the queue wrote itself, and for a
    row the verb journals under its own action (`journal == "own"`) `none`
    when that action never began and anything else when it did."""
    kind = KINDS.get(str(row.get("kind") or ""))
    state = str(journal_state or "none")
    if kind is not None and kind.journal == "own":
        return "not_delivered" if state == "none" else "own"
    if state.startswith("finished"):
        return "finished"
    if state in ("open", "interrupted"):
        return "interrupted"
    if (state == "none" and kind is not None and kind.replay
            and is_fresh(row.get("created_at"), now)):
        return "replay"
    return "not_delivered"


@dataclass(eq=False)
class Entry:
    """One thing waiting to be typed, or being typed, in a lane."""
    kind: str
    session_id: str = ""
    payload: dict = field(default_factory=dict)
    source: str = "mac"
    dedupe: str = ""
    created_at: float = 0.0
    id: str = ""
    action_id: str = ""
    replayed: bool = False
    #: Memory-only work, run in place of the kind's body.
    work: Optional[Callable[[], Awaitable]] = None
    #: A raw write: synchronous, memory-only.
    write: Optional[Callable[[], tuple]] = None
    #: Whether a row records it (set once the store has taken it).
    recorded: bool = False
    future: Optional[asyncio.Future] = None
    ready: bool = True
    gate: Optional[asyncio.Event] = None
    done_at: float = 0.0


@dataclass(frozen=True)
class EntryView:
    """A read-only look at one entry for the agent detail. `text` is the
    payload's words and is only ever turned into an excerpt for the kinds in
    `EXCERPT_KINDS` (`detail_item`)."""
    kind: str
    source: str
    session_id: str
    created_at: float
    running: bool
    text: str = ""


def _view(entry: Entry, running: bool) -> EntryView:
    text = (entry.payload or {}).get("text")
    return EntryView(
        kind=entry.kind, source=entry.source, session_id=entry.session_id,
        created_at=entry.created_at, running=running,
        text=text if isinstance(text, str) else "")


_RUNNING: contextvars.ContextVar = contextvars.ContextVar(
    "dark_army_steering_running", default=None)


class Lanes:
    """One FIFO per lane key and one worker task per non-empty lane, started
    on demand and gone when the lane is empty.

    `run(entry)` is the daemon's coroutine for one entry, answering
    `(ok, detail)`. An exception from it resolves the entry `failed` with the
    generic refusal and never kills the worker; a `BaseException` that is not
    a cancellation (a simulated crash in a test) reaches the entry's waiter
    and ends the worker.
    """

    RECENT = 16

    def __init__(self, run: Optional[Callable[[Entry], Awaitable]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_change: Optional[Callable[[], None]] = None):
        self.run = run
        self._clock = clock
        #: Called on the loop after an entry joins, starts or finishes; never
        #: awaited and never allowed to raise into a worker.
        self._on_change = on_change
        self._queues: dict = {}
        self._workers: dict = {}
        self._holds: dict = {}
        self._running: dict = {}
        self._recent: dict = {}
        self._by_id: dict = {}
        self._stopping = False

    # --- counting ------------------------------------------------------------

    def pending(self, key: str) -> int:
        """Entries in the lane, the running one included."""
        queued = sum(1 for e in self._queues.get(key, ())
                     if e.write is None)
        running = self._running.get(key)
        return queued + (1 if running is not None and running.write is None
                         else 0)

    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:
            logger.debug("steering: on_change raised", exc_info=True)

    def keys(self) -> tuple:
        """Every lane key with something queued or running."""
        found = list(self._queues)
        found.extend(k for k in self._running if k not in self._queues)
        return tuple(found)

    def held(self, key: str) -> bool:
        """The lane is paused until a deadline (a restart's settle pause)."""
        return self._holds.get(key, 0.0) > self._clock()

    def views(self, key: str) -> tuple:
        """The lane's entries in order, the running one first, as frozen
        `EntryView`s. Raw writes are left out: they carry no words and wait
        for milliseconds. A copy; the lane is never handed out."""
        out = []
        running = self._running.get(key)
        if running is not None and running.write is None:
            out.append(_view(running, True))
        out.extend(_view(e, False) for e in self._queues.get(key, ())
                   if e.write is None)
        return tuple(out)

    def full(self, key: str) -> bool:
        return self.pending(key) >= STEER_MAX_PENDING

    def has(self, entry_id: str) -> bool:
        return bool(entry_id) and entry_id in self._by_id

    def entry(self, entry_id: str) -> Optional[Entry]:
        return self._by_id.get(entry_id)

    def busy(self, key: str) -> bool:
        return key in self._workers or bool(self._queues.get(key)) \
            or key in self._holds

    def running_key(self) -> str:
        """The lane key whose worker the caller is running inside, or `""`."""
        found = _RUNNING.get()
        if found and found[0] == id(self):
            return found[1]
        return ""

    # --- submitting ----------------------------------------------------------

    def reserve(self, key: str, entry: Entry, *, force: bool = False
                ) -> Optional[asyncio.Future]:
        """Take the entry's place in the lane **now**, before anything is
        awaited, so arrival order is the order of the calls however long the
        store writes take. The worker waits at an unready entry until
        `release`. `None` when the lane is full (unless `force`)."""
        if self._stopping:
            return None
        if not force and self.full(key):
            return None
        loop = asyncio.get_running_loop()
        entry.future = loop.create_future()
        entry.ready = False
        entry.gate = asyncio.Event()
        self._queues.setdefault(key, deque()).append(entry)
        if entry.id:
            self._by_id[entry.id] = entry
        self._ensure_worker(key)
        self._changed()
        return entry.future

    def release(self, entry: Entry) -> None:
        """The entry is written down (or will go memory-only): let it run."""
        if entry.id:
            self._by_id[entry.id] = entry
        entry.ready = True
        if entry.gate is not None:
            entry.gate.set()

    def withdraw(self, entry: Entry, answer=(False, STEER_FAILED_REFUSAL)) -> None:
        """Give up a reserved place (the write that should have preceded it
        raised or was cancelled)."""
        for q in self._queues.values():
            if entry in q:
                q.remove(entry)
                break
        self._by_id.pop(entry.id, None)
        if entry.future is not None and not entry.future.done():
            entry.future.set_result(answer)
        entry.ready = True
        if entry.gate is not None:
            entry.gate.set()
        self._changed()

    def submit(self, key: str, entry: Entry, *, force: bool = False
               ) -> Optional[asyncio.Future]:
        """Queue a ready entry. `None` when the lane is full (unless `force`,
        the restart's replays, already counted by the store)."""
        future = self.reserve(key, entry, force=force)
        if future is None:
            return None
        self.release(entry)
        return future

    def offer_raw(self, key: str, write: Callable[[], tuple]) -> tuple:
        """Raw keys. On an idle, empty lane `write()` runs inline and its
        `(ok, detail)` is the answer: today's latency. Behind anything else
        the write is queued in order and the answer is `(True, "")`."""
        if not self.busy(key):
            return write()
        queued = [e for e in self._queues.get(key, ()) if e.write is not None]
        if len(queued) >= STEER_MAX_RAW_PENDING or self._stopping:
            return False, STEER_FULL_REFUSAL
        entry = Entry(kind="raw", write=write, created_at=time.time())
        self.submit(key, entry, force=True)
        return True, ""

    def hold_until(self, key: str, deadline: float) -> None:
        """Nothing in the lane starts before the monotonic `deadline`; later
        submits queue behind."""
        self._holds[key] = max(self._holds.get(key, 0.0), float(deadline))
        self._ensure_worker(key)

    def find_replay_twin(self, key: str, kind: str, payload: dict,
                         source: str, now: float) -> Optional[Entry]:
        """A queued or recently finished entry of this lane that was replayed
        after a restart and says the same thing, for a person at the Mac who
        presses again what Dark Army is already replaying. Never matches two
        live presses."""
        if source != "mac":
            return None
        words = str((payload or {}).get("text") or "").strip()
        pool = list(self._queues.get(key, ()))
        running = self._running.get(key)
        if running is not None:
            pool.append(running)
        pool.extend(self._recent.get(key, ()))
        for entry in pool:
            if (entry.replayed and entry.kind == kind
                    and entry.source == "mac"
                    and str((entry.payload or {}).get("text") or "").strip()
                    == words
                    and 0 <= now - entry.created_at <= STEER_REPLAY_FRESH_SECONDS):
                return entry
        return None

    # --- the worker ----------------------------------------------------------

    def _ensure_worker(self, key: str) -> None:
        task = self._workers.get(key)
        if task is not None and not task.done():
            return
        self._workers[key] = asyncio.get_running_loop().create_task(
            self._work(key))

    async def _work(self, key: str) -> None:
        token = _RUNNING.set((id(self), key))
        try:
            while True:
                hold = self._holds.get(key, 0.0)
                now = self._clock()
                if hold > now:
                    await asyncio.sleep(hold - now)
                    continue
                self._holds.pop(key, None)
                queue = self._queues.get(key)
                if not queue:
                    break
                entry = queue[0]
                if not entry.ready and entry.gate is not None:
                    await entry.gate.wait()
                    # It may have been withdrawn while we waited.
                    if not queue or queue[0] is not entry:
                        continue
                queue.popleft()
                self._running[key] = entry
                self._changed()
                try:
                    await self._run_one(entry)
                finally:
                    self._running.pop(key, None)
                    self._by_id.pop(entry.id, None)
                    entry.done_at = time.time()
                    if entry.replayed:
                        recent = self._recent.setdefault(
                            key, deque(maxlen=self.RECENT))
                        recent.append(entry)
                    self._changed()
        finally:
            _RUNNING.reset(token)
            if self._workers.get(key) is asyncio.current_task():
                self._workers.pop(key, None)
            if not self._queues.get(key):
                self._queues.pop(key, None)

    async def _run_one(self, entry: Entry) -> None:
        future = entry.future
        try:
            if entry.write is not None:
                answer = entry.write()
            elif self.run is None:
                answer = (False, STEER_FAILED_REFUSAL)
            else:
                answer = await self.run(entry)
        except asyncio.CancelledError:
            if future is not None and not future.done():
                # Cancelled mid-entry: part of its words may have landed.
                future.set_result((False, STEER_INTERRUPTED_REFUSAL))
            raise
        except Exception:
            logger.warning("steering: a %s entry raised", entry.kind,
                           exc_info=True)
            answer = (False, STEER_FAILED_REFUSAL)
        except BaseException as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            raise
        if future is not None and not future.done():
            future.set_result(answer)

    # --- stopping ------------------------------------------------------------

    async def drained(self) -> None:
        """Wait until every lane is empty and every worker has exited."""
        while self._workers:
            await asyncio.wait(list(self._workers.values()))
        await asyncio.sleep(0)

    async def stop(self, grace: float = STEER_SHUTDOWN_GRACE_SECONDS) -> None:
        """Queued entries are dropped from memory (a durable one stays
        `queued` on disk for the next launch's freshness rule); the running
        one gets `grace` to finish and is then cancelled."""
        self._stopping = True
        for key, queue in list(self._queues.items()):
            for entry in list(queue):
                queue.remove(entry)
                self._by_id.pop(entry.id, None)
                if entry.future is not None and not entry.future.done():
                    entry.future.set_result((False, STEER_RESTARTING_NOTE
                                            if entry.recorded
                                            else STEER_SHUTDOWN_REFUSAL))
                if entry.gate is not None:
                    entry.gate.set()
        self._holds.clear()
        tasks = []
        for key, task in list(self._workers.items()):
            if task.done():
                continue
            if key in self._running:
                tasks.append(task)
            else:
                # Asleep in a hold or at a gate: nothing is typing.
                task.cancel()
                tasks.append(task)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=max(0.0, grace))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)


# --- the agent detail (`docs/steering-queue.md`, *What the agent detail shows*)

DETAIL_MAX_ITEMS = 6
DETAIL_EXCERPT_CHARS = 80
DETAIL_WHY_CHARS = 200
UNDELIVERED_PER_SESSION = 8
UNDELIVERED_MAX_SESSIONS = 64
#: How long a not-delivered entry stays on the row (memory only); the
#: retention the store keeps its resolved rows for, imported so they agree.
UNDELIVERED_SECONDS = STEER_RETENTION_SECONDS

#: The only kinds whose words (clamped) ride the snapshot. `terminal_line`
#: and `raw` can land in a password prompt, `answer` and `permission_key` are
#: digits, the rest are fixed commands.
EXCERPT_KINDS = frozenset({"reply", "card_message", "card_ask"})
#: Left out of the list altogether: they wait milliseconds, and `raw` is one
#: frame per keystroke.
DETAIL_HIDDEN_KINDS = frozenset({"raw", "permission_key"})
SOURCE_WORDS = {"phone": "the phone", "mac": "the Mac"}
STATUS_WORDS = {
    "typing": "being typed now",
    "waiting": "waiting its turn",
    "not_delivered": "not delivered",
    "interrupted": "may only partly have landed \u2014 check the terminal",
}
#: The sentence a dropped entry resolves with (the tree's own).
RESTART_WHY = STEER_NOT_DELIVERED_WHY


def excerpt(text) -> str:
    """One line of at most `DETAIL_EXCERPT_CHARS` characters: whitespace runs
    collapsed, cut with an ellipsis. `""` for anything that is not text."""
    if not isinstance(text, str):
        return ""
    line = " ".join(text.split())
    if len(line) > DETAIL_EXCERPT_CHARS:
        return line[:DETAIL_EXCERPT_CHARS - 1] + "\u2026"
    return line


def detail_item(*, kind, source, created_at, group, state, text="",
                why="") -> dict:
    """One list entry, every word composed here. Exactly
    `{"group", "label", "status", "excerpt", "why"}`."""
    noun = WORDS.get(kind, "a message")
    noun = noun[:1].upper() + noun[1:]
    origin = ("Dark Army" if kind in ("autocompact", "limit_resume")
              else SOURCE_WORDS.get(source, "the Mac"))
    try:
        clock = time.strftime("%H:%M", time.localtime(float(created_at)))
    except (TypeError, ValueError, OverflowError, OSError):
        clock = ""
    label = f"{noun} from {origin}"
    if clock:
        label += f", {clock}"
    status = STATUS_WORDS.get(
        "not_delivered" if state in ("refused", "failed") else state,
        STATUS_WORDS["not_delivered"])
    why_line = " ".join(str(why or "").split())
    if len(why_line) > DETAIL_WHY_CHARS:
        why_line = why_line[:DETAIL_WHY_CHARS - 1] + "\u2026"
    return {"group": str(group), "label": label, "status": status,
            "excerpt": excerpt(text) if kind in EXCERPT_KINDS else "",
            "why": why_line}


def compose_detail(waiting: list, undelivered: list) -> Optional[dict]:
    """The row's `message_queue`: waiting entries in lane order, then the
    undelivered newest first, cut to `DETAIL_MAX_ITEMS`. `None` for nothing.
    Exactly `{"headline", "items", "more_line"}`."""
    if not waiting and not undelivered:
        return None
    items = list(waiting) + list(undelivered)
    shown = items[:DETAIL_MAX_ITEMS]
    parts = []
    if waiting:
        parts.append(f"{len(waiting)} waiting to be typed")
    if undelivered:
        parts.append(f"{len(undelivered)} not delivered")
    rest = len(items) - len(shown)
    return {"headline": " \u00b7 ".join(parts), "items": shown,
            "more_line": f"and {rest} more" if rest > 0 else ""}


def listable(views, held: bool) -> list:
    """The views of one lane worth showing: none when a lone entry is being
    typed or about to be (an ordinary reply, so the page never flickers on
    one), everything once a second entry waits behind it or a restart's
    settle pause holds the lane."""
    shown = [v for v in views if v.kind not in DETAIL_HIDDEN_KINDS]
    if len(shown) >= 2 or (shown and held):
        return shown
    return []
