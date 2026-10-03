"""The board half of `BobDaemon` — every Kanban verb, moved whole.

`daemon.py` passed ten thousand lines, and the board — the store calls, the
dispatch/refine/queue machinery, the reconcile, the channel-request handlers
and the parallel dial — is the one seam that lifts out without changing a
line of behaviour. `BobDaemon` inherits `BoardVerbsMixin`; every method here
still runs as a bound method of the daemon, against attributes initialised
in `BobDaemon.__init__`. **Pure move**: the bodies below are verbatim from
`daemon.py`, with one mechanical exception, `_D` (next paragraph).

This module must not import `daemon` at module import time — `daemon.py`
imports it to build the class. Module-level constants and functions the
moved code uses (`PLAN_GATE_REFUSAL`, `_queue_reason`, …) deliberately stay
*defined in `daemon.py`*: tests and the phone's source pins read them there,
and a monkeypatch on `dark_army_daemon.daemon.<name>` must keep biting.
`_D` resolves those names through the daemon module at call time, which is
exactly what the same bare name did when the code lived there.
"""
import asyncio
import ctypes
import functools
import hashlib
import logging
import os
import secrets
import stat as stat_mod
import subprocess
import threading
import sys
import time
from pathlib import Path
from typing import Optional

from . import agent_models
from . import agent_report
from . import agents_poll
from . import attachments
from . import areas
from . import board
from . import board_queue
from . import board_workflow
from . import card_priority
from . import card_prepare
from . import channel_server
from . import dispatch
from . import enrollment
from . import grok_roster
from . import identity
from . import inbox_ack
from . import manual_check
from . import merges
from . import mission
from . import origin
from . import paths
from . import plan_index
from . import run_figures as run_figures_mod
from . import run_health
from . import scout_index
from . import scout_report
from . import session_io
from . import subprocess_env
from . import trust_marks
from . import work_record
from . import workspace
from . import worktrees

#: How long one `depends_on` reference (a card id or an exact title) may be
#: before it is clamped: `board.MAX_TITLE_CHARS`, so any stored title can be
#: named exactly, and `channel_server.ADD_TOOL`'s `maxLength`, which clamps
#: first on the way in.
MAX_DEPENDENCY_REF_CHARS = 200
#: `_decorate_card_for_snapshot`'s "no `dep_running_ids` handed in" marker —
#: distinct from None, which is `_dependency_running_ids`' "unknown".
_RUNNING_FROM_FRAME = object()

#: `open_mission` while the pty broker link is down. The terminal map says
#: nothing mid-reconnect (`PtyHost.connected`), so spawning would risk a
#: second Mission Control beside one the broker still holds. Transient in
#: its words, never in `dispatch._TRANSIENT_REFUSALS` — no queue holds it.
MISSION_HOST_RECONNECTING_REFUSAL = (
    "Dark Army is reconnecting to its terminals — try again in a moment")
#: `open_mission` with no checkout to run in: Mission Control reads plans
#: and the docs off Dark Army's own source tree, and an installed
#: release with no `repo-root` stamp has none.
MISSION_NO_CHECKOUT_REFUSAL = "Mission Control needs Dark Army's own checkout"
#: `open_mission` when that checkout is not enrolled — the ad-hoc rule.
MISSION_NOT_ENROLLED_REFUSAL = (
    "Dark Army's own checkout is not enrolled — enrol it under Projects")
#: `end_mission` with nothing to end.
MISSION_NOT_RUNNING_REFUSAL = "Mission Control is not running"
#: `end_mission` when the terminal at the recorded handle no longer wears
#: Mission Control's pty name, or its bound session was not started as
#: Mission Control: the record is stale and the close would aim at somebody
#: else's terminal.
MISSION_IDENTITY_REFUSAL = (
    "that terminal is no longer Mission Control's — nothing was closed")
#: `end_mission` when the broker did not confirm the close — an RPC timeout
#: or a lost link. The record is kept: the process may well be alive, and a
#: record blanked here would let the next open start a second one.
MISSION_CLOSE_FAILED_REFUSAL = (
    "Dark Army could not confirm the close — Mission Control may still be "
    "running; try again")
#: The `open_mission` reply detail while a live one is already there.
MISSION_ALREADY_RUNNING = mission.ALREADY_RUNNING



#: The single-flight refusal `prepare_card_text` answers while a draft is
#: still being written. One spelling: `api_server._prepare` answers it with
#: a 202 the receipt ledger skips, so a phone replaying a lost reply is never
#: handed this sentence as its recorded answer.
PREPARE_BUSY_DETAIL = "Dark Army is already writing one — give it a moment"


def ask_direct_tail(name) -> str:
    """Direction that rides in a direct-route push, not only in the channel's
    instructions — a long-lived channel process predates any instructions
    change and would otherwise receive the question with no way back.

    Named for the session's own registration (`channel_server.NAMES`): a
    session born under the legacy name has `bob_answer_card`, a new one
    `dark_army_answer_card`. An unknown or missing name is the legacy one,
    because only a copy from before the dual-name window omits it.
    """
    tool = channel_server.tool_name("answer_card", name)
    return (f"Answer this question about your card through {tool}. "
            "Write one answer; change nothing else.")


def _find_own_checkout() -> str:
    """Dark Army's own checkout as `dev_build.find_repo_root` verifies it —
    the same root `enrollment.enroll_self` enrols and `pack_install`'s
    `_is_bobs_own` tests — or `""`. Imported inside, as `enroll_self` does:
    the daemon reaches into the menu-bar package for this one read only.

    **Never a card's side folder**: an app run from one finds it by the
    ancestor walk, and Done later removes the folder and takes back its key —
    Mission Control opened there lost the board mid-run. `main_checkout`
    answers the main checkout, as `enroll_self` and the build stamp do."""
    try:
        from dark_army_menubar import dev_build
        root = dev_build.find_repo_root()
        if root:
            root = dev_build.main_checkout(root)
    except Exception:
        logger.debug("could not locate Dark Army's own repo root",
                     exc_info=True)
        return ""
    return str(root) if root else ""

#: More crew files than this in one folder and the copy test gives up and
#: keeps the folder: it is a bound on the walk, not a verdict.
_CREW_COPY_LIMIT = 2000


def _crew_output_is_copied(output: bytes, path: str, root: str) -> bool:
    """Whether every file `argv_crew_output` names inside `path` already
    exists byte-identical at the same place in `root`, the main checkout.
    **Executor only** (it reads files). A check flagged from a side folder
    is copied to the main checkout and the original stays behind; without
    this the folder was kept on every Done, for good, over a duplicate.
    Anything unreadable, a link, an entry outside the folder or more than
    `_CREW_COPY_LIMIT` files answers False — keep, as before."""
    base = os.path.realpath(str(path or ""))
    home = os.path.realpath(str(root or ""))
    if not base or not home or base == home:
        return False
    files: list = []
    for entry in bytes(output or b"").split(b"\0"):
        rel = os.fsdecode(entry[3:]).rstrip("/") if len(entry) > 3 else ""
        if not rel:
            continue
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep):
            return False
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                if any(os.path.islink(os.path.join(dirpath, d))
                       for d in dirnames):
                    return False
                files.extend(os.path.join(dirpath, f) for f in filenames)
                if len(files) > _CREW_COPY_LIMIT:
                    return False
        else:
            files.append(full)
    if not files:
        return False
    for full in files:
        if os.path.islink(full):
            return False
        twin = os.path.join(home, os.path.relpath(full, base))
        try:
            with open(full, "rb") as a, open(twin, "rb") as b:
                if a.read() != b.read():
                    return False
        except OSError:
            return False
    return True


def _mission_folder_is_stale(root: str) -> bool:
    """True when a live Mission Control terminal runs in a folder that is not
    Dark Army's own checkout as `_find_own_checkout` reads it now. **Executor
    only.** An unreadable checkout answers False — keep what runs, as an
    unreadable brief does — so only a known mismatch replaces the session."""
    home = _find_own_checkout()
    if not home or not root:
        return False
    return dispatch.normalise_root(str(root)) != dispatch.normalise_root(home)


#: The same logger object `daemon.py` writes to — one stream, one name.
logger = logging.getLogger("dark-army")


class _Statfs(ctypes.Structure):
    """Darwin's 64-bit-inode `struct statfs` (`<sys/mount.h>`), the layout
    `statfs` has on arm64 and `statfs$INODE64` on x86_64."""
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16),
                ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024),
                ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


def setup_volume_type(path: str) -> str:
    """The kind of disk `path` is on — `statfs(2)`'s `f_fstypename`
    (`apfs`, `hfs`, `msdos`, `smbfs`, …) — or `""` when it cannot be read.
    **Executor only** (a system call on a path). The setup script's gate
    refuses anything but `apfs` (`worktrees.setup_volume_allows`), so `""`
    fails closed. The one seam a test stubs to stand in for another disk."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            call = libc["statfs$INODE64"]
        except AttributeError:
            call = libc["statfs"]
        call.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
        call.restype = ctypes.c_int
        buf = _Statfs()
        if call(os.fsencode(str(path)), ctypes.byref(buf)) != 0:
            return ""
        return buf.f_fstypename.decode("ascii", "replace")
    except (OSError, AttributeError, TypeError, ValueError):
        logger.debug("could not read the disk kind of %s", path,
                     exc_info=True)
        return ""


class _DaemonModule:
    """Call-time lookup of names defined at module level in `daemon.py`.

    An attribute read here is what a bare global read was before the move:
    resolved against `dark_army_daemon.daemon` at the moment of use, so
    a test that patches that module still reaches the moved code."""

    def __getattr__(self, name):
        return getattr(sys.modules["dark_army_daemon.daemon"], name)


_D = _DaemonModule()


def _plan_digest(path: str) -> str:
    """The SHA-256 hex of a plan file's bytes, or `""`.

    **Blocking (a read and a hash) — executor only.** Read through
    `board_workflow.read_plan_text`, which is the one bounded reader for a
    plan, so an oversize or unreadable file digests as `""` and therefore can
    never accidentally equal a stored approval — failure is no evidence, this
    codebase's standing rule, and here that direction means *the gate fires*
    rather than *the gate is skipped*.
    """
    text = board_workflow.read_plan_text(Path(path))
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plan_state(path: str) -> tuple:
    """`(exists, digest)` for one plan path, in one executor hop.

    Both facts the gate needs, read together: the gate used to cost a single
    `os.path.isfile` hop and must still cost exactly one, not two.
    """
    if not os.path.isfile(path):
        return False, ""
    return True, _plan_digest(path)


#: How long Mission Control's ask to start a card stays on Needs you. An
#: hour: long enough to be seen from the phone, short enough that a stale
#: ask does not sit there for a day. Held in memory only — a daemon restart
#: drops every ask, and Mission Control asks again.
START_ASK_TTL_SECONDS = 3600.0
#: At most this many asks at once; a ninth is refused, not queued.
MAX_START_ASKS = 8
START_ASK_BY = "Mission Control"

# --- Batch refinement: several Prep cards, one planning session ------------
# The words a batch press is refused in, and the note an unplanned member is
# left with. Module constants for `START_ASK_BY`'s reason: the tests and the
# API layer quote them rather than retyping prose.
BATCH_TOO_FEW_REFUSAL = "tick at least two Prep cards to refine them together"
BATCH_TOO_MANY_REFUSAL = ("one planning session takes at most {limit} cards "
                          "— refine the rest in a second batch")
BATCH_MIXED_ROOT_REFUSAL = ("{title} is in a different project — a batch "
                            "refines the cards of one project")
BATCH_MIXED_TOOL_REFUSAL = ("{title} names a different assistant — a batch "
                            "runs in one session, so every card must name the same one")
BATCH_NAMED_PLAN_REFUSAL = ("{title} already names a finished plan — press "
                            "Refine on it alone")
BATCH_ATTACH_AMBIGUOUS_REFUSAL = (
    "this session is refining several cards and the plan does not say which "
    "one it is for — add `- **Card:** <id>` to the plan's header and attach "
    "again. The cards:\n{members}")
BATCH_UNPLANNED_NOTE = ("card {rank} of a batch planning session, and its "
                        "plan never arrived — press Refine again")

# --- Batch implementation: several Backlog cards, one session, one at a time -
# `plans/2026-09-25-batch-implement-backlog-cards.md`. The session holds
# `session_id` on exactly one open card at a time; the members still to come
# wait in Backlog carrying only `batch_id` / `batch_rank`.
BATCH_START_TOO_FEW_REFUSAL = ("tick at least two planned Backlog cards to "
                               "start them together")
BATCH_START_TOO_MANY_REFUSAL = ("one session takes at most {limit} cards — "
                                "start the rest in a second batch")
BATCH_START_MIXED_ROOT_REFUSAL = ("{title} is in a different project — a "
                                  "batch starts the cards of one project")
BATCH_START_MIXED_TOOL_REFUSAL = ("{title} names a different assistant — a "
                                  "batch runs in one session, so every card "
                                  "must name the same one")
BATCH_TOOL_REFUSAL = ("grok has no board tools, so it cannot close cards one "
                      "by one — start them singly")
BATCH_NOT_BACKLOG_REFUSAL = "not in Backlog"
BATCH_QUEUED_REFUSAL = "already queued"
BATCH_SCOUT_REFUSAL = ("a scout ends in a report, not a build — start it "
                       "on its own")
BATCH_MEMBER_REFUSAL = ("this card left its batch's line but still carries "
                        "the batch — press Leave batch, then Start it")
BATCH_WAITING_REFUSAL = ("this card is waiting its turn in a batch — Leave "
                         "batch first")
BATCH_DEPENDENCY_REFUSAL = ("it waits on a card that is not finished — start "
                            "it once that one is done")
BATCH_NO_PLACE_REFUSAL = ("no free place in {project} — wait for a run to "
                          "finish or raise RUN")
BATCH_NOT_BOUND_REFUSAL = "Dark Army could not tell which session asked"
BATCH_NO_BATCH_REFUSAL = "this session is not working a batch card"
BATCH_BUSY_REFUSAL = ("this session is on more than one card — close it on "
                      "the board")
BATCH_LEFT_NOTE = ("the batch ended before this card was started — press "
                   "Start to run it alone")


def _batch_waiting(card: Optional[dict]) -> bool:
    """Whether this card is a batch member still waiting its turn. Pure.

    The one definition every rung reads — the single Start's refusal, the
    advance's candidates, the snapshot's `waiting` state and the store's
    `release_batch_waiting` WHERE clause say the same thing: the mark, no
    session, no link and Backlog. A refinement batch's Prep cards never
    match (their column is `prep` and their link is `refine_state`), and a
    Done card dragged back keeps its `session_id`, so it is not waiting.
    """
    card = card or {}
    return (bool(str(card.get("batch_id") or ""))
            and not str(card.get("session_id") or "")
            and not str(card.get("link_state") or "")
            and str(card.get("column_name") or "") == "backlog"
            and str(card.get("refine_state") or "") not in ("dispatching",
                                                            "live"))


def _skip_lines(head: str, skips: list) -> str:
    """A batch press's refusal with the cards it skipped named under it,
    one `<title> — <refusal>` line each, `_start_project_report`'s cap.
    Pure."""
    lines = [str(head or "").strip()]
    limit = 8
    for title, detail in list(skips or [])[:limit]:
        lines.append(f"{str(title or '').strip() or 'untitled'} \u2014 "
                     f"{str(detail or '').strip()}")
    if len(skips or []) > limit:
        lines.append(f"\u2026and {len(skips) - limit} more")
    return "\n".join(lines)


def _batch_marked_unbound(card: Optional[dict]) -> bool:
    """A card carrying a batch-implement mark with no session of its own, in
    **any** column. Pure. A single Start on one is refused: moving a waiting
    member out of Backlog keeps its mark, and starting it alone would put a
    second session on a card the batch's own session will bind. The head
    while it is still binding (`dispatching`) and a refinement batch's
    Prep cards (`refine_state`) are the other verbs' to refuse."""
    card = card or {}
    return (bool(str(card.get("batch_id") or ""))
            and not str(card.get("session_id") or "")
            and str(card.get("link_state") or "") != "dispatching"
            and str(card.get("refine_state") or "") not in ("dispatching",
                                                            "live"))


def _batch_owner(members: list) -> Optional[dict]:
    """The member that speaks for a batch-implement batch, or None. Pure.

    The lowest-ranked member that has a session, or is still binding
    (`dispatching`). The press binds its session to rank 1, and the advance
    only ever binds the next *waiting* member — always a higher rank than
    any card that session already holds — so this is that session's card
    for the life of the batch. It does not need rank 1 to still exist: a
    person may delete or reopen the finished first card mid-batch and the
    session keeps its batch. A session forced onto a member dragged out of
    Backlog holds a higher rank than the batch's own, and never owns it.
    """
    for member in sorted(members or [], key=_batch_rank):
        if str(member.get("session_id") or "") \
                or str(member.get("link_state") or "") == "dispatching":
            return member
    return None


def _batch_rank(card: Optional[dict]) -> int:
    """A member's 1-based place in its batch, `0` when unreadable. Pure."""
    try:
        return int(str((card or {}).get("batch_rank") or "0").strip() or "0")
    except ValueError:
        return 0



#: Held across the outcome press's place check, header re-read and write, so
#: two presses on one check cannot both see `Status: open` and both write.
#: One lock for every check: a press is a person's, and they are rare.
_MANUAL_OUTCOME_LOCK = threading.Lock()

class BoardVerbsMixin:
    """The board verbs of `BobDaemon`. Never instantiated on its own."""

    # --- The board (see board.py for the store, dispatch.py for the launcher) ---

    #: How long a card's session may be absent from the fleet before the card
    #: says the session ended. Above the hysteresis flicker and above a daemon
    #: restart's reload of sessions.json; below FINISHED_RETENTION_SECONDS, so
    #: the card is marked while the tombstone is still there to explain it.
    BOARD_SESSION_GRACE = 90.0
    #: How much of the Done column rides in the snapshot. The rest is fetched on
    #: demand (`GET /api/board`) — this payload goes out on the SSE stream, and a
    #: board used for a year must not grow every frame.
    BOARD_DONE_WINDOW = 24 * 3600.0
    BOARD_DONE_LIMIT = 50
    #: How much of a card's prompt rides in the snapshot.
    #:
    #: `board.py` measured the problem and named this as the fix: every card's
    #: **full** prompt was going out on `/api/state`, and therefore on every SSE
    #: frame — 20 cards at the store's 8,000-character limit is 165 KB, against
    #: an `api_server` limiter tuned around a ~19 KB snapshot. The store's bound
    #: was never the wrong bound; carrying it in the live payload was.
    #:
    #: 400 is enough for a card to be recognised in a column, which is all the
    #: board draws. Anything that needs the real text — the editor — fetches it
    #: from `GET /api/board`, which is untouched and still serves it in full.
    BOARD_SNAPSHOT_PROMPT_CHARS = 400
    #: Same treatment for `summary`. The store now allows a paragraph
    #: (`board.MAX_SUMMARY_CHARS`); the live payload still carries 400, and
    #: the editor fetches the rest before Save is allowed.
    BOARD_SNAPSHOT_SUMMARY_CHARS = 400
    #: Seconds after a prepare finishes before another may start. Stops a
    #: double-press paying twice; `_preparing` covers the overlapping case.
    PREPARE_COOLDOWN = 5.0

    async def _board_call(self, method: str, *args, **kwargs):
        """One board statement, on the executor. `None` when the board is shut.

        The hop is not optional and not a tidy-up: sqlite3 blocks, and this loop
        also drives the menu-bar strip, the panel's stream and the terminal
        titles. `_history_write` makes the same hop for the same reason; the
        difference is that this one is awaited, because a board verb has to
        report whether it worked."""
        if self._board is None:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(getattr(self._board, method), *args, **kwargs))

    def _log_card_event(self, card, kind: str, session_id: str = "",
                        **detail) -> None:
        """One diary line about a card (`event_log`), the display fields read
        off the card dict the store just returned — `project` and `title` are
        already resolved there — and the nickname off `_nicknames_shown` for
        the session named. Named detail keys only. Safe from either thread:
        `_log_event` hops or appends as the caller's thread dictates."""
        capture = getattr(self, "_decisions", None)
        if capture is not None and isinstance(card, dict):
            capture.submit("card_event", dict(card, session_id=session_id or card.get("session_id", "")), kind, detail)
        if kind == "card_manual_clear":
            return
        if getattr(self, "_event_log", None) is None or not isinstance(card, dict):
            return
        sid = str(session_id or card.get("session_id") or "")
        self._log_event(
            kind,
            project=str(card.get("project") or ""),
            title=str(card.get("title") or ""),
            card_id=str(card.get("id") or ""),
            session_id=sid,
            nickname=self._nicknames_shown.get(sid, "") if sid else "",
            detail=detail)

    def _build_board_state(self) -> dict:
        """The board as every surface sees it. **Blocking** — call it on the
        executor or from a thread that is already off the loop.

        Held in `_board_state` rather than computed per request: `ApiServer.state`
        runs once per SSE frame, and a database read on that path would put
        SQLite on the event loop several times a second.
        """
        if self._board is None:
            return {"generated_at": time.time(), "cards": [],
                    # Compatibility alias: `ready` copies `backlog`. Not a
                    # fourth column — COLUMNS stays three names.
                    "counts": board.with_ready_alias(
                        {c: 0 for c in board.COLUMNS}),
                    "dispatch_enabled": False, "autostart_enabled": False,
                    "prepare_enabled": False,
                    # Present even with the board shut, for `counts`' reason:
                    # a surface never handles a missing key, and a limit of
                    # zero is not a state the readout may ever draw.
                    "parallel_limit": int(self.board_parallel_limit),
                    # And which projects say otherwise. For the picker's
                    # tick-state alone — never a denominator: what a project
                    # actually runs on arrives per card.
                    "parallel_overrides": dict(self.board_parallel_overrides),
                    # The projects whose card isolation is switched off, for
                    # the switch's tick-state; what a card's own project is on
                    # rides per card as `isolation`.
                    "isolation_overrides": dict(
                        getattr(self, "board_isolation_overrides", None) or {}),
                    "available": False,
                    "tools": list(dispatch._EXECUTABLES),
                    "installed": dispatch.installed_tools(), "projects": [],
                    # The model catalogue rides beside `tools` for the same
                    # reason and under `counts`' never-missing-key rule. It is
                    # static — identical JSON every frame — so it never differs
                    # in the clock-stripped `_news` comparison and never makes
                    # a frame news; that is also why it is not in
                    # `api_server._CLOCK_FIELDS`, which is for fields that
                    # change without being news. This one never changes.
                    "models": {t: list(m) for t, m in dispatch.MODELS.items()},
                    "done_clear_token": "",
                    "done_view_token": "",
                    **self._pipeline_writable()}
        try:
            done_count, done_clear_token, done_view_token = \
                self._board.done_tokens()
            cards = [c for c in self._board.cards() if c.get("column_name") != "done"]
            # A close nobody has acknowledged must never age out of the live
            # board: `done_since` is windowed at 24 h, and a card an assistant
            # closed on Friday must still wear its "FINISHED · REVIEW" banner
            # on Monday. Bounded at the same limit as the recent preview so the
            # frame's worst case stays stated; deduped by id, because a fresh
            # close is legitimately in both reads.
            #
            # Read **first**, so the union can record which leg produced each
            # Done card: a card the awaiting-review read returned is one that
            # still wants a person and rides every frame, and a card only the
            # recent-preview read returned is stamped `done_preview` and is
            # what a client opting into `?done=review` stops receiving. The
            # stamp is a record of the read, never a second judgment —
            # `done_awaiting_review`'s SQL is the only place that decides.
            cards.extend(self._board.done_awaiting_review(
                self.BOARD_DONE_LIMIT))
            seen_ids = {c["id"] for c in cards if c.get("id")}
            # A finished card whose manual check is still open wants a person
            # too, however old it is: it rides every frame, so the Needs you
            # entry every client draws is one the daemon can acknowledge.
            for row in self._board.done_manual_check_pending(
                    self.BOARD_DONE_LIMIT):
                if (row.get("id") and row["id"] not in seen_ids
                        and str(row.get("manual_steps") or "").strip()):
                    seen_ids.add(row["id"])
                    cards.append(row)
            for row in self._board.done_since(
                    time.time() - self.BOARD_DONE_WINDOW,
                    self.BOARD_DONE_LIMIT):
                if not row.get("id") or row["id"] in seen_ids:
                    continue
                row["done_preview"] = True
                seen_ids.add(row["id"])
                cards.append(row)
            by_id = {c["id"]: c for c in cards if c.get("id")}
            # One reading of "which sessions can Dark Army still hear" for the whole
            # frame: per card it would be a set rebuilt thirty times, and two
            # readings inside one snapshot could disagree with each other.
            active = self._claiming_session_ids()
            # How full each project's pipeline is, once per frame off the same
            # reading. A queued card's sentence is composed from this number —
            # under the slot rule there is no single holder to name, the
            # project is simply full — and computing it per card would be the
            # same walk thirty times over with no chance of two answers
            # disagreeing inside one snapshot.
            running_by_project: dict = {}
            for row in cards:
                if board_queue.holds(row, active):
                    key = str(row.get("project") or "")
                    running_by_project[key] = running_by_project.get(key, 0) + 1
            # Which sessions are *working*, once per frame off the same
            # snapshot the rail draws. Empty before the first agents push, so
            # a flagged live card badges on for the seconds until one arrives
            # — self-correcting and stated in the plan's Risks rather than
            # special-cased into a third state.
            running_ids = self._board_running_ids(
                self._agents_snapshot_cache or {})
            # One reading for the frame, on `active`'s own rule: the two are
            # asked together on every card, and two walks inside one snapshot
            # are two chances to publish a card as lost and working at once.
            mid_turn = self._mid_turn_quiet_ids()
            thread_counts = self._board.message_counts()
            # One reading of every finished run's headline for the whole
            # frame — eight scalars per card, ~110 bytes of JSON, and the
            # report text and file list deliberately not among them. Nothing
            # in it moves except when a run actually ends, so it never churns
            # a frame past the broadcast limiter's quiet floor and it needs no
            # `_CLOCK_FIELDS` entry.
            run_heads = self._board.run_headlines()
            # And one reading of every card's cost-and-time parts off the two
            # retained ledgers, plus the live context percent per session off
            # the same agents snapshot the rail draws. Composed per card at
            # decorate time (`run_figures.compose`) so the minute floor, the
            # cent and the whole percent bound what can change between
            # frames; the frame itself is bought by `_run_figures_drifted`.
            figure_parts = self._board.run_figures()
            ctx_by_session = self._live_ctx_by_session(
                self._agents_snapshot_cache or {})
            # One read of the two run-health ledgers (attempts, returns)
            # and one memoised load of the frozen readings per frame, on
            # `run_heads`' argument: per card it would be thirty reads that
            # could disagree inside one snapshot. The figures are quantised
            # in `run_health.compose`, so a live run does not churn the
            # frame on every turn.
            run_counts = self._board.run_health_counts(
                [c.get("id") for c in cards])
            run_ledger = run_health.Ledger.load()
            # Every card's dependencies resolved once for the frame — one
            # `cards_by_id` read for the ones outside it — and the reverse
            # map for "Unblocks", on `run_heads`' one-read-per-frame rule.
            dep_by_id, dependents = self._dependency_frame(cards)
            dep_running = self._dependency_running_ids(
                self._agents_snapshot_cache)
            # How many cards each batch-implement press marked, once per
            # frame, so every member's "n of m" reads one count.
            batch_sizes: dict = {}
            for row in cards:
                bid = str(row.get("batch_id") or "")
                if bid:
                    batch_sizes[bid] = batch_sizes.get(bid, 0) + 1
            cards = [self._decorate_card_for_snapshot(
                self._trim_card_for_snapshot(c), by_id, active,
                running_by_project, running_ids, run_heads, mid_turn,
                run_figures=figure_parts, ctx_by_session=ctx_by_session,
                run_counts=run_counts, run_ledger=run_ledger,
                dep_by_id=dep_by_id, dependents=dependents,
                batch_sizes=batch_sizes, dep_running_ids=dep_running)
                for c in cards]
            for card in cards:
                card["thread_count"] = int(
                    thread_counts.get(card.get("id") or "", 0))
            # Compatibility alias: `ready` is a copy of `backlog`, not a
            # fourth column. Surfaces shipped against schema 1 still index it.
            counts = board.with_ready_alias(self._board.counts())
            # Done previews are bounded, but the destructive scope is not.
            # Publish its count and exact-membership token from the same store
            # read so the two confirmation fields can never disagree.
            counts["done"] = done_count
        except Exception:
            logger.warning("could not read the board", exc_info=True)
            return dict(self._board_state) or {"generated_at": time.time(),
                                               "cards": [], "counts": {},
                                               "dispatch_enabled": False,
                                               "prepare_enabled": False,
                                               "autostart_enabled": False,
                                               "parallel_limit": int(
                                                   self.board_parallel_limit),
                                               "parallel_overrides": dict(
                                                   self.board_parallel_overrides),
                                               "isolation_overrides": dict(
                                                   getattr(self, "board_isolation_overrides", None) or {}),
                                               "available": False, "tools": [],
                                               "installed": dispatch.installed_tools(),
                                               "models": {},
                                               "done_clear_token": "",
                                               "done_view_token": "",
                                               **self._pipeline_writable()}
        return {
            "generated_at": time.time(),
            "cards": cards,
            "counts": counts,
            "done_clear_token": done_clear_token,
            # What a *held* copy of the Done column can go stale against —
            # membership, any revised column, and a review. Beside the
            # membership token rather than folded into it: `clear_done`
            # compares that one and only that one, and a content-sensitive
            # membership token would refuse a Clear Done because somebody
            # retitled a finished card.
            "done_view_token": done_view_token,
            # Whether Start may appear at all. In the payload rather than left to
            # the panel's copy of the preference, so the button is *absent* when
            # Dark Army may not launch — never present and inert.
            "dispatch_enabled": bool(self.board_dispatch_enabled),
            # Whether Prepare may appear at all. `dispatch_enabled`'s
            # pattern for the phone, which has no stdin context to gate on:
            # with the helper off the button is *absent*, never inert.
            "prepare_enabled": True,
            # Whether a queued card may promise that Dark Army will start it. In the
            # payload for `dispatch_enabled`'s reason, applied to a *promise*
            # rather than to a button: with the drain off, a queued card must
            # say "press Start" instead of "Dark Army will
            # start it" — a card promising an action Dark Army will not take is
            # worse than no queue at all.
            "autostart_enabled": bool(self.board_autostart_enabled),
            # How many agents may work at once in one project — the waiting
            # rule itself, published so the Pipeline readout draws "RUN 2/3"
            # without re-deriving anything. The **clamped attribute**, never
            # `preferences.json`: a hand-edited 50 would otherwise draw a
            # limit the gate does not obey.
            "parallel_limit": int(self.board_parallel_limit),
            # Which projects have said otherwise, for the heading picker's
            # tick-state alone. The number a project actually runs on rides
            # per card as `parallel_limit`, already resolved; this map is
            # never a denominator.
            "parallel_overrides": dict(self.board_parallel_overrides),
            # Which projects have switched card isolation off
            # (`docs/card-worktrees.md`), for the switch's tick-state alone:
            # each card publishes its own project's resolved `isolation`.
            "isolation_overrides": dict(
                getattr(self, "board_isolation_overrides", None) or {}),
            "available": True,
            "outcomes_supported": True,
            "lifecycle_available": not getattr(self, "_lifecycle_unavailable", False),
            **self._pipeline_writable(),
            # The assistants Dark Army can actually start. Anything Dark Army knows how
            # to launch is here; a tool it knows *of* but cannot launch is in
            # `dispatch._UNSUPPORTED`, offered on the card and refused in
            # words at Start, rather than silently missing from the chooser.
            "tools": list(dispatch._EXECUTABLES),
            "installed": dispatch.installed_tools(),
            # And which models each of them may be run on. Published rather
            # than duplicated in Swift, so the panel re-derives nothing: the
            # daemon's snapshot stays the single source of truth for what a
            # card may name, exactly as `tools` is for who may take it.
            "models": {t: list(m) for t, m in dispatch.MODELS.items()},
            # The projects a card may be filed against, with the folder each
            # would dispatch into. Published rather than left to the board to
            # guess, because the guard refuses a root it does not recognise —
            # and a folder typed by hand is refused at the moment somebody
            # presses Start, which is the worst place to learn it was wrong.
            "projects": self._board_projects(),
        }

    def _pipeline_writable(self) -> dict:
        """Which pipeline controls this Mac will honour from a phone.

        Several flags rather than one, because they are false for different
        reasons and a surface that conflated them would hide a control that
        works. `queue_writable` is a **version marker** — this daemon carries
        `board_unqueue` / `board_queue_move` on the phone's own tuples, and an
        older one simply sends no key, which decodes false.
        `preferences_writable` is a *live* fact: the two dials are applied by
        the menu-bar app, so a daemon running headless has nobody to hand a
        press to — and that is exactly the condition under which
        `request_preference` would refuse one.

        Published on all three snapshot shapes under this file's own
        never-a-missing-key rule (`counts`' reason): a surface must never have
        to handle an absent key, and absent must never read as a control that
        is present and inert.
        """
        return {
            "queue_writable": True,
            # A third, at v15, on `queue_writable`'s own argument: this daemon
            # carries `board_approve_plan` on the phone's tuples and an older
            # one simply sends no key, which decodes false — so the Approve
            # control is drawn *absent* rather than present and inert.
            "plan_approval_supported": True,
            # And a fourth, on the same argument: this daemon carries
            # `board_start_project` on the phone's tuples, and an older one
            # simply sends no key — which decodes false, so START PROJECT is
            # drawn *absent* rather than present and 404ing inside the sealed
            # reply.
            "start_project_writable": True,
            # And a fifth, on `queue_writable`'s argument once more: this
            # daemon carries `board_message` on both phone tuples, and an
            # older one simply sends no key — which decodes false, so SEND A
            # MESSAGE is drawn *absent* rather than present and 404ing inside
            # the sealed reply.
            "card_message_writable": True,
            # And a twelfth, on `queue_writable`'s argument once more: this
            # daemon serves the sealed `card_changes` read, and carries
            # `board_merge`, `board_merge_fix` and `board_review_run` on both
            # phone tuples. Three markers, so a phone draws the Changes view
            # against a Mac that has it and each press against a Mac that
            # honours it; an older Mac sends none, which decodes false, so
            # all of it is drawn *absent* rather than present and 404ing
            # inside the sealed reply (`docs/card-worktrees.md`, *Review and
            # merge*).
            "card_changes_supported": True,
            "merge_writable": True,
            "review_run_writable": True,
            # And a sixth, on `queue_writable`'s argument once more: this
            # daemon knows the `start_when_planned` card column, and an
            # older one simply sends no key — which decodes false, so the
            # tick is drawn *absent* rather than present and 400ing inside
            # `_board_named_fields`, which refuses an update whose named
            # fields were all dropped.
            "start_when_planned_supported": True,
            # And a seventh, on `queue_writable`'s argument once more: this
            # daemon knows the `priority` card column, and an older one
            # simply sends no key — which decodes false, so the number box
            # is drawn *absent* rather than present and 400ing inside
            # `_board_named_fields`, which refuses an update whose named
            # fields were all dropped.
            "priority_supported": True,
            "areas_supported": True,
            # And on the same argument: this daemon decides and publishes
            # `run_health` per card. The phone draws the line only where
            # this key is true, so against an older Mac it shows nothing
            # rather than a broken line; the panel decodes no markers and
            # draws the line whenever the card carries one.
            "run_health_supported": True,
            # And on `queue_writable`'s argument once more: this daemon serves
            # the sealed `agent_report` read, and an older one simply sends no
            # key — which decodes false, so the phone draws the agent report
            # section **absent** rather than present and 404ing inside the
            # sealed reply.
            "agent_report_supported": True,
            # And on `queue_writable`'s argument once more: this daemon serves
            # the sealed `lifecycle` read, and an older one simply sends no
            # key — which decodes false, so the phone draws the timing
            # section **absent** rather than present and 404ing.
            "lifecycle_supported": True,
            # And on `queue_writable`'s argument once more: this daemon
            # serves the sealed `history_week` read, and an older one simply
            # sends no key — which decodes false, so the phone's History
            # tile stays dim with a sentence rather than lit and 404ing.
            "history_week_supported": True,
            # And on the same argument once more: this daemon admits the
            # three objective text fields on a sealed `board_create`, and an
            # older one answers 403 for the *whole* create — so the phone
            # draws the three boxes *absent* rather than present and refused.
            "objective_on_create_supported": True,
            # And an eighth, on `queue_writable`'s argument once more: this
            # daemon serves the sealed `terminal` read and carries
            # `terminal_input` on both phone tuples; an older one simply
            # sends no key — which decodes false, so the phone's terminal
            # screen is drawn *absent* rather than present and 404ing.
            "terminal_supported": True,
            # And beside it: this daemon serves the sealed, held-open
            # `POST /api/terminal/stream` on the phone door, paints on
            # `since_bytes=-1`, and takes raw `bytes` on `terminal_input`.
            # An older Mac sends no key — which decodes false — so the phone
            # draws the terminal *absent* (one sentence) rather than a
            # blank emulator waiting on a route that is not there.
            "terminal_stream_supported": True,
            # And a ninth, on `queue_writable`'s argument once more: this
            # daemon carries `board_clear_done` on both phone tuples, and an
            # older one simply sends no key — which decodes false, so the
            # button is drawn *absent* rather than present and 404ing
            # inside the sealed reply.
            "clear_done_writable": True,
            # Two more on `clear_done_writable`'s argument: this daemon
            # carries `board_manual_clear` and `board_review` on both phone
            # tuples, each taking a current-state echo. Two markers, not
            # one, so a phone can hide one control on its own against a
            # Mac that honours the other; an older Mac sends neither key,
            # which decodes false, so Mark checked / Mark reviewed are drawn
            # *absent* rather than present and 404ing.
            "manual_clear_writable": True,
            "review_writable": True,
            # The Review section (docs/review-runs.md): this daemon serves
            # `state.review` and GET /api/review and carries three review
            # actions on both phone tuples. Two markers on `queue_writable`'s
            # argument: the name "Mark reviewed" took above is taken, so the
            # press marker is `review_section_writable` (`review_run_writable`
            # is the Done card's Run review). An older Mac sends neither,
            # which decodes false, so the tile says so in a sentence.
            "review_supported": True,
            "review_section_writable": True,
            # Two more on the same argument: this daemon serves the Checks
            # section (GET /api/manual-checks and the sealed `manual_checks`
            # kind) and carries `board_manual_outcome` on both phone tuples.
            # Two markers, so a phone can draw the list against a Mac that
            # refuses the press; an older Mac sends neither key, which
            # decodes false, so the Menu tile is dim and Passed / Failed are
            # drawn *absent* rather than present and 404ing.
            "manual_checks_supported": True,
            "manual_outcome_writable": True,
            # Where the next Start opens its terminal — Dark Army's own pty or the
            # project's VS Code window. A live fact for the panel's caption;
            # the tick itself lives in the ⋯ menu.
            "own_terminal_enabled": bool(
                getattr(self, "board_own_terminal_enabled", False)),
            # A tenth, on `queue_writable`'s argument once more: this daemon
            # honours `own_terminal` on `board_dispatch`, so a card with no
            # connected terminal can spawn one inside Dark Army even when
            # the machine-wide preference is off. An older one ignores the
            # flag, so START HERE is drawn *absent* rather than present and
            # opening the editor instead.
            "own_terminal_spawn_supported": True,
            # An eleventh, on `queue_writable`'s argument once more: this
            # daemon carries the `spawn_terminal` verb on its loopback door,
            # so an assistant can be started in an enrolled folder with no
            # card behind it. An older one simply sends no key — which
            # decodes false, so + TERMINAL is drawn *absent* rather than
            # present and 400ing. Loopback only: the key is published on
            # every snapshot shape under this file's never-a-missing-key
            # rule, and the phone has no gesture that asks for it.
            "adhoc_terminal_supported": True,
            # The human knowledge reader: this daemon serves GET /api/knowledge
            # and the sealed `knowledge` kind. An older one sends no key —
            # which decodes false, so Settings → Knowledge and the phone
            # screen are drawn *absent* rather than present and empty.
            "knowledge_supported": True,
            # And on `queue_writable`'s argument once more: this daemon
            # serves the scout-report list and one report's body
            # (`GET /api/scout-reports`, `GET /api/scout-report`, the
            # sealed `scout_reports` / `scout_report` kinds). An older one
            # sends no key — which decodes false — so the phone's Scouting
            # tile stays dim rather than present and empty.
            "scout_reports_supported": True,
            # And on `queue_writable`'s argument once more: this daemon
            # searches the reports' bodies when the list read carries `q`
            # (`scout_index.search`). An older one sends no key — which
            # decodes false — so the phone asks for no body search and
            # keeps its instant search alone.
            "scout_reports_body_search_supported": True,
            # The same argument for the plan list and one plan's text
            # (`GET /api/plans`, `GET /api/plan`, the sealed `plans` /
            # `plan` kinds). An older one sends no key — which decodes
            # false — so the phone's Plans tile stays dim rather than
            # present and empty.
            "plans_supported": True,
            # This daemon logs refused knocks on the phone doors and serves
            # the log (`GET /api/access-log`, the sealed `access_log` kind).
            # An older one sends no key — which decodes false — so the
            # phone's Profile row is drawn *absent* rather than empty.
            "access_log_supported": True,
            # This daemon times every sealed request on both phone doors,
            # echoes its legs on the reply envelope and serves ten-minute
            # rollups behind `?timing=1` on the access-log read. An older
            # one sends no key — which decodes false — so the phone asks
            # for no rollup and draws its own side alone.
            "link_timing_supported": True,
            # Desktop confirm / edit / mark-stale. The phone has no write
            # controls and reads `knowledge_supported` only.
            "knowledge_writable": True,
            # And on `queue_writable`'s argument once more: this daemon's
            # on-open card read carries a `timeline` beside `plan`, and an
            # older one simply sends no key — which decodes false, so the
            # phone draws the TIMELINE row *absent* rather than empty.
            "card_timeline_supported": True,
            # And on `queue_writable`'s argument once more: this daemon
            # stamps `run_figures` — cost so far, working minutes, live
            # context, attempts — on every card that has had a run, and an
            # older one simply sends no key, which decodes false, so the
            # phone draws the figures line *absent* rather than blank.
            "run_figures_supported": True,
            # And once more: this daemon takes `register_activity_token` and
            # pushes Live Activity updates through the mailbox, and an older
            # one simply sends no key, which decodes false, so the phone
            # starts and ends its live card locally and posts no token.
            "live_activity_supported": True,
            # And once more: this daemon publishes the fleet Lock Screen
            # card — counts, cost, tokens, the waiter on top. An older one
            # sends no key, which decodes false, so the phone keeps the
            # face-only card and the two ends agree on when it is up.
            "live_activity_fleet": True,
            # The four-section briefing Mission Control reads, and that a
            # paired phone may fetch through its sealed door. An older Mac
            # sends no key — which decodes false — so the phone draws
            # nothing rather than 404ing the kind.
            "bearings_supported": True,
            # This daemon takes `kind` on a phone's `board_create`
            # (`""` build / `"scout"`); an older one drops the key and
            # writes a build card, so the phone draws its Build / Scout
            # control **absent** rather than present and silently ignored.
            "scout_supported": True,
            # This daemon honours `board_promote` from a paired phone (it
            # was the Mac's alone in v1); an older one 404s the verb, so
            # the phone draws Promote absent rather than present and refused.
            "promote_supported": True,
            # This daemon carries `board_refine_batch` on both phone tuples;
            # an older one 404s the verb (and sends no key, which decodes
            # false), so the phone draws the Prep row's Select control
            # **absent** rather than present and refused.
            "refine_batch_supported": True,
            # This daemon carries `board_start_batch` on both phone tuples;
            # an older one 404s the verb (and sends no key, which decodes
            # false), so the phone draws the Backlog row's Select control
            # **absent** rather than present and refused.
            "start_batch_supported": True,
            # This daemon carries `rebuild_app` on both phone tuples; an
            # older one 404s it from away and sends no key, which decodes
            # false, so the phone draws the away tile dim rather than
            # offering a press that would be refused.
            "rebuild_away_supported": True,
            # This daemon serves the sealed `conversation` read — a session's
            # turns, paged by a cursor. An older Mac sends no key, which
            # decodes false, so the phone opens Details and draws one
            # sentence rather than a blank conversation.
            "conversation_supported": True,
            # This daemon takes `agent` on the `conversation` read — one
            # helper's own journal. An older one ignores the key and would
            # page the parent into the helper's cache, so the phone draws
            # no helper tabs without it.
            "subagent_conversation_supported": True,
            # This daemon gates Start on `blocked_by`, admits it through
            # `board_update`, and publishes `dependencies` / `dependents` and
            # their two lines per card. An older Mac sends no key, which
            # decodes false, so the phone draws its WAITS ON editor **absent**
            # rather than writing a list that Mac would drop at the door.
            "dependencies_supported": True,
            "preferences_writable": bool(
                self._observers_implementing("on_preference_request")),
        }

    @classmethod
    def _trim_card_for_snapshot(cls, card: dict) -> dict:
        """A card as the live board draws it: everything except the whole prompt
        and the whole summary.

        Copied rather than mutated — `self._board.cards()` hands back a fresh
        dict per read today, but a store that ever caches would otherwise find
        its rows truncated in place, and the bug would surface as *the editor
        saving 400 characters over somebody's instructions*. Copy once, then
        clamp each field independently; an untruncated field carries **no**
        flag key.

        `prompt_truncated` / `summary_truncated` are stated by the daemon
        rather than left to the panel to infer from a length: the editor
        blocks Save on each flag until it has fetched the real text, and a
        client guessing "400 characters, probably cut" would be wrong on both
        sides — a genuinely 400-character value is whole, and a limit that
        moves here would silently un-guard every editor in the field.
        """
        prompt = str(card.get("prompt") or "")
        summary = str(card.get("summary") or "")
        prompt_cut = len(prompt) > cls.BOARD_SNAPSHOT_PROMPT_CHARS
        summary_cut = len(summary) > cls.BOARD_SNAPSHOT_SUMMARY_CHARS
        # Outcome text is on-demand; SSE carries only its status/revision.
        # Retired folder columns stay in SQLite for a downgrade and never
        # ride the snapshot this build publishes.
        out = {k: v for k, v in card.items() if k not in
               ("beneficiary", "intended_benefit", "success_criterion",
                "outcome_check_on", "initiative_id", "initiative_by")}
        if prompt_cut:
            out["prompt"] = prompt[:cls.BOARD_SNAPSHOT_PROMPT_CHARS]
            out["prompt_truncated"] = True
        if summary_cut:
            out["summary"] = summary[:cls.BOARD_SNAPSHOT_SUMMARY_CHARS]
            out["summary_truncated"] = True
        return out

    def _decorate_card_for_snapshot(self, card: dict, by_id: dict,
                                    active: Optional[set] = None,
                                    running_by_project: Optional[dict] = None,
                                    running_ids: Optional[set] = None,
                                    run_heads: Optional[dict] = None,
                                    mid_turn: Optional[set] = None,
                                    *, run_figures: Optional[dict] = None,
                                    ctx_by_session: Optional[dict] = None,
                                    run_counts: Optional[dict] = None,
                                    run_ledger=None,
                                    dep_by_id: Optional[dict] = None,
                                    dependents: Optional[dict] = None,
                                    batch_sizes: Optional[dict] = None,
                                    dep_running_ids=_RUNNING_FROM_FRAME,
                                    worktree_cards=None
                                    ) -> dict:
        """Derived fields the board draws: nickname, closer, queue words.

        A sibling of `_trim_card_for_snapshot`, not a mutation of it — trim is
        a classmethod and must not grow a dependency on the identity store.
        Copies always: trim already copies when it truncates, and must still
        copy when it returned the original so a later decorate cannot write
        through into a cached row.

        `peek`, never `name_for`: assigning here would dirty identities.json
        for every dead session that ever filed a card.
        """
        out = dict(card)
        author = str(out.get("author") or "")
        if author in ("", "user"):
            out["author_name"] = ""
        else:
            out["author_name"] = self._identities.peek(author)
        closed_by = str(out.get("closed_by") or "")
        out["closed_by_name"] = (
            "" if not closed_by else self._identities.peek(closed_by))
        # What a queued card says about its wait, in the daemon's own words.
        # Composed here rather than in the panel so the sentence on the badge
        # and the sentence in the refusal the press answered with are the same
        # one; composed *now* rather than remembered from the reconcile so
        # flipping `board_autostart` rewords every queued card on the next
        # frame. No holder is named because under the slot rule none exists:
        # the project is full, not a particular card in the way.
        #
        # Written onto the copy, never the store's row — `out` is already a
        # `dict(card)` for exactly this reason.
        if str(out.get("queue_state") or "") == "queued":
            running = int((running_by_project or {}).get(
                str(out.get("project") or ""), 0))
            out["queue_reason"] = _D._queue_reason(
                running, bool(self.board_autostart_enabled))
        # Two answers about the same card, published rather than re-derived.
        # `work_active` is the queue gate's own per-card answer (`holds`),
        # kept on the wire with its meaning unchanged for installed panels:
        # a card whose session has finished and gone quiet — or that is
        # flagged for a hand-check — stops holding the project's files.
        # `run_active` is the band's RUN/DONE split: whether an assistant is
        # actually working, which does *not* release on `manual_steps` — a
        # flagged card whose session is still busy is running, not done.
        # One reading of the active set for both, matching the caller's
        # one-set-per-frame rule.
        sessions = self._claiming_session_ids() if active is None else active
        out["work_active"] = board_queue.holds(out, sessions)
        out["run_active"] = board_queue.run_active(out, sessions)
        # Whether the hand-check note is *somebody's turn yet*. Decided here
        # rather than in the panel for the reason `run_active` is: the panel's
        # own rows call a roster-only stub `running`, so a Swift-side join
        # against the fleet would hide the badge on exactly the
        # finished-and-quiet card that most needs it. Only the daemon holds the
        # hook-stream freshness reading (`_claiming_session_ids`).
        #
        # The steps term is the invariant — a badge with nothing behind it
        # cannot exist — and it is asserted again panel-side, because the wire
        # is not a place to trust an invariant to.
        # How many agents may work at once in *this* card's project — the
        # daemon's own resolution of the machine dial against the project's
        # override, published rather than joined in Swift for `run_active`'s
        # reason: the panel would otherwise need the override map, the root
        # canonicaliser and the floor, and three copies of a rule is three
        # chances to draw a denominator the gate does not obey.
        out["parallel_limit"] = self._parallel_limit_for(out.get("root"))
        # Card isolation (`docs/card-worktrees.md`): whether a Start in this
        # card's project works in its own worktree — `"on"` / `"off"` for a
        # git project, `""` where there is no checkout to isolate — resolved
        # here for `parallel_limit`'s reason. And one line in the daemon's
        # words while its folder is being prepared, or where a finished
        # card's folder was kept because it held unsaved work; absent
        # otherwise.
        out["isolation"] = self._isolation_state(out.get("root"))
        wt_note = self._worktree_note(
            out, by_id, cards=worktree_cards)
        if wt_note:
            out["worktree_note"] = wt_note
        # Review and merge (`docs/card-worktrees.md`): the four keys, where
        # non-empty, composed here and drawn verbatim by both clients.
        self._decorate_merge(out)
        # Mission Control asked for this card to be started. Published only
        # while the ask is fresh and the card could still take a Start —
        # a card already started, starting or finished shows no ask, so the
        # Needs you entry goes the moment somebody presses START.
        ask = self._live_start_ask(out)
        if ask is not None:
            out["start_ask_id"] = ask["id"]
            out["start_asked_at"] = ask["asked_at"]
            out["start_asked_by"] = START_ASK_BY
        out["manual_check_due"] = (
            bool(str(out.get("manual_steps") or "").strip())
            and not self._card_session_working(
                out, running_ids or set(), sessions))
        # The cards this one waits on and the cards waiting on it, composed
        # here and drawn verbatim by both clients (`docs/card-dependencies.md`).
        # Resolved through `_dependency_entries` with the same `running_ids`
        # and `sessions` the badge above just used, so a dependency reads as
        # met on this tile at the instant its own MANUAL CHECK badge lights
        # — and the gate and the drain ask the same resolver. **All four keys
        # absent where empty**, `work_record`'s rule. `dep_by_id` is the
        # frame's resolution; without it (no caller today) the frame's
        # `by_id` is read with the store fallback.
        if out.get("blocked_by"):
            entries = self._dependency_entries(
                out, dep_by_id if dep_by_id is not None else by_id,
                (running_ids or set()) if dep_running_ids is _RUNNING_FROM_FRAME
                else dep_running_ids, sessions,
                fetch=dep_by_id is None)
            if entries:
                out["dependencies"] = entries
                out["dependency_line"] = _D._dependency_line(entries)
                unmet = [e["title"] for e in entries if not e["met"]]
                # A held queued card says what it is waiting for, not how
                # full its project is: the dependency, not the slot, is what
                # stands between it and a terminal.
                if unmet and str(out.get("queue_state") or "") == "queued":
                    out["queue_reason"] = _D._dependency_reason(
                        unmet, bool(self.board_autostart_enabled))
        kids = (dependents or {}).get(str(out.get("id") or ""))
        if kids:
            out["dependents"] = [dict(k) for k in kids]
            out["dependents_line"] = _D._dependents_line(
                [k.get("title") for k in kids])
        # Published rather than joined in Swift for run_active's reason:
        # only the daemon has the hook-stream freshness reading — and, with
        # it, the reading that says which quiet session was mid-turn when the
        # silence started, without which this bit calls a working assistant
        # a lost one.
        out["needs_you"] = board_queue.needs_you(
            out, sessions,
            self._mid_turn_quiet_ids() if mid_turn is None else mid_turn)
        # What Dark Army wrote down about this card's last finished run — the
        # headline alone. **Absent where there is no record**, which is the
        # load-bearing half: a card nobody has run and a run that changed
        # nothing are different facts, and an empty object here would make
        # them the same one on every surface. The whole record — the closing
        # words and the file list — never rides SSE; it is fetched per card.
        head = (run_heads or {}).get(str(out.get("id") or ""))
        if isinstance(head, dict):
            out["work_record"] = dict(head)
        # What this card has cost so far and how long its assistant has
        # actually worked — `work_record`'s own rule one line up: **absent
        # where the card has no run row and no execution span**, so a card
        # nobody has ever run looks exactly as it did. The parts come from
        # the two retained ledgers (`BoardStore.run_figures`), the context
        # percent from the agents snapshot for the bound session and only
        # while the link is live; a Codex card, whose spend Dark Army cannot
        # measure, carries `cost_usd` None and coverage "unknown" rather
        # than a made-up zero.
        parts = (run_figures or {}).get(str(out.get("id") or ""))
        if isinstance(parts, dict):
            ctx = None
            if str(out.get("link_state") or "") == "live":
                ctx = (ctx_by_session or {}).get(
                    str(out.get("session_id") or ""))
            out["run_figures"] = run_figures_mod.compose(
                cost=parts.get("cost") or {},
                seconds=parts.get("seconds"),
                ticking=bool(parts.get("ticking")),
                ctx=ctx,
                attempts=int(parts.get("attempts") or 0))
        # Who was on each observed stage. **Absent where there is none**, on
        # `work_record`'s own rule one line up: a card nobody has worked and a
        # card whose crew is empty must not become the same fact on the wire.
        # Cheap (a handful of short pairs) and it moves only when a stage is
        # first recorded, so it adds no churn to `_news`. A slug the 22 Sep
        # 2026 cast rebrand retired is shown as its successor; the stored
        # `crew_trail` is never rewritten (`identity.current_crew`).
        faces = identity.current_crew(board.parse_crew(out.get("crew_trail")))
        if faces:
            out["crew"] = faces
        # The character Start will hand this card, published so the face on
        # the card is the face that starts (`_promised_lead`). Absent where
        # the card has no area pool or is Done; a client with no key draws
        # the area's usual lead as before.
        lead = self._promised_lead(out)
        if lead:
            out["lead_face"] = lead
        # How the run is going, decided here and drawn verbatim by both
        # clients. **Absent where none**, on `work_record`'s rule: a card
        # nobody has started carries no key. The row is this card's
        # session's in the cached agents snapshot — the rows the rail draws,
        # the one source of `stats`, `metrics`, `spawn_counts` and the three
        # counters — joined through `_snapshot_row_in`; a card whose session
        # is gone reads its frozen entry, `live` false. The cuts come off the
        # ledger's frozen readings of the card's own root, so a live run
        # never moves its own bar.
        reading = self._run_health_for(out, run_counts, run_ledger)
        if reading is not None:
            out["run_health"] = reading
        # Where this card stands in a batch-implement session, drawn by the
        # panel as "BATCH 2/3 · waiting". **Absent where none**, on
        # `work_record`'s rule: only the member being worked and the ones
        # still waiting carry it — a refinement batch's Prep cards, a
        # member the session moved past and a finished one carry nothing.
        # `size` is the frame's one count (`batch_sizes`); `batch_id` itself
        # rides as opaque bookkeeping and no client decodes it.
        batch = self._batch_mark(out, batch_sizes)
        if batch is not None:
            out["batch"] = batch
        return out

    @staticmethod
    def _batch_mark(card: dict, batch_sizes: Optional[dict]) -> Optional[dict]:
        """`{rank, size, state}` for a batch member being worked, waiting,
        or dragged out of the line, else None. Pure. `working`: In
        progress, and either still binding (`dispatching`) or bound `live`;
        `waiting`: `_batch_waiting`; `left`: the mark with no session outside
        Backlog (`_batch_marked_unbound`) — a card whose single Start the
        daemon refuses until Leave batch, so the tile must say so."""
        bid = str(card.get("batch_id") or "")
        if not bid:
            return None
        link = str(card.get("link_state") or "")
        state = ""
        if str(card.get("column_name") or "") == "in_progress" and (
                link == "dispatching"
                or (link == "live" and str(card.get("session_id") or ""))):
            state = "working"
        elif _batch_waiting(card):
            state = "waiting"
        elif _batch_marked_unbound(card):
            state = "left"
        if not state:
            return None
        rank = _batch_rank(card)
        size = int((batch_sizes or {}).get(bid) or 0)
        return {"rank": rank, "size": max(size, rank), "state": state}

    def _run_health_for(self, card: dict, run_counts: Optional[dict],
                        run_ledger, snapshot: Optional[dict] = None
                        ) -> Optional[dict]:
        """`run_health.compose` for one card off the cached agents snapshot
        — or off `snapshot` where the caller holds the one this pass was
        handed (`_run_health_drifted`, inside the reconcile). Executor only,
        no I/O: the ledger was loaded once by the caller. Never raises — a
        broken figure is a missing line, not a missing board."""
        cid = str(card.get("id") or "")
        if not cid:
            return None
        try:
            ledger = run_ledger if run_ledger is not None \
                else run_health.Ledger.load()
            counts = (run_counts or {}).get(cid)
            if counts is None and self._board is not None:
                counts = self._board.run_health_counts([cid]).get(cid)
            if snapshot is None:
                snapshot = getattr(self, "_agents_snapshot_cache", None) or {}
            row = self._snapshot_row_in(
                snapshot, str(card.get("session_id") or ""))
            entry = ledger.entry_for(cid)
            if row is None and entry is None:
                return None
            cuts = ledger.cuts_for(str(card.get("root") or ""))
            return run_health.compose(row, counts, entry, cuts)
        except Exception:
            logger.debug("run health for %s unavailable", cid, exc_info=True)
            return None

    def _freeze_run_health(self, card: dict, snapshot: dict) -> None:
        """Write the run's final reading down at the one ended-run seam.
        Executor; the file write is `atomic_write_json` off the loop.
        Idempotent on the card's `dispatched_at` **and the reading**
        (`Ledger.freeze` decides: the identical reading writes nothing, so
        a restart's re-run of the grace writes nothing twice, while a later
        quiet spell of an `ended → live` resume — same `dispatched_at`,
        more turns — replaces the first). Reads the row off the snapshot
        the reconcile was handed, as `_consider_work_record` does, so one
        pass is consistent with itself."""
        card = card if isinstance(card, dict) else {}
        cid = str(card.get("id") or "")
        sid = str(card.get("session_id") or "")
        root = str(card.get("root") or "")
        if not cid or not sid:
            return
        try:
            run_at = float(card.get("dispatched_at") or 0.0)
        except (TypeError, ValueError):
            run_at = 0.0
        if not run_at:
            return
        try:
            ledger = run_health.Ledger.load()
            row = self._snapshot_row_in(snapshot, sid)
            if row is None:
                return
            counts = None
            if self._board is not None:
                counts = self._board.run_health_counts([cid]).get(cid)
            reading = run_health.compose(row, counts, None,
                                         ledger.cuts_for(root))
            if reading is None:
                return
            ledger.freeze(cid, root, reading, run_at)
        except Exception:
            logger.warning("could not freeze run health for %s", cid,
                           exc_info=True)

    def _board_projects(self) -> list:
        """Every open VS Code window as `{name, root}`. Blocking (it reads the
        extension locks, cached 5s); executor only, like its caller.

        `workspace.py` is this app's definition of a project and it is reused
        here rather than re-derived — the same list `dispatch._known_project_roots`
        checks against, so what the board offers is what dispatch will accept.
        """
        out = []
        seen = set()
        try:
            for window in workspace.windows():
                for folder in window.folders:
                    root = dispatch.normalise_root(folder)
                    if not root or root in seen:
                        continue
                    seen.add(root)
                    out.append({"name": window.name, "root": root})
        except Exception:
            logger.debug("could not list projects for the board", exc_info=True)
        return out

    def _refresh_board_state(self) -> dict:
        """Recompute and hold the board payload. Blocking; executor only."""
        self._board_state = self._build_board_state()
        return self._board_state

    def done_archive_cards(self, limit: int = 1000) -> dict:
        """The whole Done column, in the snapshot's own shape. **Blocking
        (SQLite) — executor only.**

        `GET /api/board?range=` already serves finished cards, but as *raw
        store rows*: no `work_record`, no `closed_by_name`, no
        `thread_count`. A client that has to splice these into `board.cards`
        needs the shape the frame uses, or it needs a second decode path and
        two ideas of what a card is. So this is the frame's own pipeline —
        trim, decorate, thread count — run over the archive instead of over
        the preview.

        **The tokens are read before the cards, and the order is the whole
        point.** A card written between the two reads leaves the caller
        holding a copy stamped with an older token: it refetches once more
        than it had to. The other order leaves a client holding a stale copy
        stamped fresh — for ever, because nothing would ever move it again.
        """
        if self._board is None:
            return {"available": False, "cards": [], "count": 0,
                    "generated_at": time.time(), "more": False,
                    "done_clear_token": "", "done_view_token": "",
                    "reason": "the board is not open"}
        count, clear_token, view_token = self._board.done_tokens()
        rows = self._board.done_since(0.0, int(limit))
        more = len(rows) >= int(limit)
        # One reading each for the whole fetch — the frame's own rule, for
        # the frame's own reason: per card these would be the same walk once
        # per row, with two readings inside one answer free to disagree.
        active = self._claiming_session_ids()
        mid_turn = self._mid_turn_quiet_ids()
        run_heads = self._board.run_headlines()
        # The finished totals, the same read the frame takes. The context
        # map is empty on purpose: a Done card is never live.
        figure_parts = self._board.run_figures()
        thread_counts = self._board.message_counts()
        run_counts = self._board.run_health_counts(
            [c.get("id") for c in rows])
        run_ledger = run_health.Ledger.load()
        by_id = {c["id"]: c for c in rows if c.get("id")}
        dep_by_id, dependents = self._dependency_frame(rows)
        dep_running = self._dependency_running_ids(self._agents_snapshot_cache)
        # The kept-folder note asks whether a card sharing the folder is
        # still at work, and that card is never in this Done page: the
        # whole board, read once, only when a row names a folder.
        share_cards = (self._board.cards()
                       if any(r.get("worktree_path") for r in rows) else None)
        cards = []
        for row in rows:
            card = self._decorate_card_for_snapshot(
                self._trim_card_for_snapshot(row), by_id, active,
                None, None, run_heads, mid_turn,
                run_figures=figure_parts, ctx_by_session={},
                run_counts=run_counts, run_ledger=run_ledger,
                dep_by_id=dep_by_id, dependents=dependents,
                dep_running_ids=dep_running, worktree_cards=share_cards)
            card["thread_count"] = int(
                thread_counts.get(card.get("id") or "", 0))
            cards.append(card)
        return {"available": True, "generated_at": time.time(),
                "cards": cards, "count": count, "more": more,
                "done_clear_token": clear_token,
                "done_view_token": view_token}

    def _sweep_orphan_attachments(self) -> int:
        """Drop staging folders no card names, older than a day.

        Blocking — directory walk — so `run` hops it to the executor. The
        age guard is what protects a composer that is open right now: those
        copies are referenced by no card yet.
        """
        if self._board is None:
            return 0
        keep = attachments.referenced_folders(self._board.cards())
        return attachments.sweep_orphans(keep)

    def _backfill_board_workflows(self) -> int:
        """Fill evidence-backed expectations before the first board snapshot.

        Blocking by construction: project discovery, bounded plan reads and
        SQLite all happen here, and ``run`` sends the whole operation to the
        executor. Safe to repeat on every launch; the store's conditional write
        makes an existing declaration authoritative.

        The roster filter is the same one `create_card` applies, and it has to
        be here too: this runs on every launch, so without it a card whose
        `workflow` somebody *cleared* — the documented cure for an off-roster
        helper — would be re-seeded from the plan document's header with that
        same off-roster name on the next daemon restart. The roster is read
        once per distinct root; `card_prepare.read_roster` returns `[]` rather
        than raising, and an empty roster means today's unfiltered resolve.
        """
        if self._board is None:
            return 0
        known_roots = self._known_project_roots()
        rosters: dict[str, list] = {}
        changed = 0
        for card in self._board.cards():
            # Per card, because card text is writable by anything on the
            # machine through `dark_army_add_card` and this loop runs inside `run`'s
            # board `try`: one card that made the resolver raise used to set
            # `self._board = None` for the whole daemon run, which took the
            # board down *and* removed the only surface that could delete the
            # offending card.
            try:
                objective_empty = not all(
                    card.get(k) for k in ("beneficiary", "intended_benefit",
                                          "success_criterion"))
                if card.get("plan_path") and (not card.get("area") or objective_empty):
                    resolved = board_workflow.resolve_plan_file(
                        card["plan_path"], card.get("root"), known_roots)
                    if resolved is not None:
                        if not card.get("area"):
                            slug = board_workflow.read_plan_area(resolved)
                            _, did_fill = self._board.fill_area_if_empty(card["id"], slug)
                            changed += int(did_fill)
                        if objective_empty:
                            objective = board_workflow.read_plan_objective(resolved)
                            _, did_fill = self._board.fill_objective_if_empty(
                                card["id"], objective)
                            changed += int(did_fill)
                if board.parse_stages(card.get("workflow")):
                    continue
                root = str(card.get("root") or "")
                if root not in rosters:
                    rosters[root] = [
                        name for name, _desc
                        in card_prepare.read_roster(root)
                    ]
                stages = board_workflow.resolve(
                    card, known_roots, rosters[root])
                if not stages:
                    continue
                _, did_fill = self._board.fill_workflow_if_empty(
                    card["id"], stages)
                changed += int(did_fill)
            except Exception:
                logger.warning("could not resolve stages for card %s",
                               card.get("id"), exc_info=True)
                continue
        return changed

    def _notify_board(self) -> None:
        """Hand the held payload to whoever draws it. Pure dispatch — no I/O, so
        it is safe on the loop, which is where every observer expects to be
        called."""
        self._notify_observers("on_board_change", self._board_state)

    async def _publish_board(self) -> None:
        """Recompute on the executor, announce on the loop."""
        if self._board is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._refresh_board_state)
        self._notify_board()

    def _known_project_roots(self) -> set:
        """Every folder a card may be dispatched into. **Blocking** (it reads the
        extension locks) — executor only.

        Two sources, unioned: the folders of every open VS Code window, which is
        this app's definition of a project (`workspace.py`, reused rather than
        re-derived), and the cwd of every live session, which covers a window
        whose lock is stale but whose agents are plainly working in it.
        """
        roots = set()
        try:
            for window in workspace.windows():
                for folder in window.folders:
                    roots.add(dispatch.normalise_root(folder))
        except Exception:
            logger.debug("could not read VS Code windows for dispatch",
                         exc_info=True)
        for name, group in (self._agents_snapshot_cache or {}).items():
            if not isinstance(group, list):
                continue
            for row in group:
                if not isinstance(row, dict):
                    continue
                # A tombstone carries the folder its run happened in (that is
                # what freezes a finished row's project label), but a run that
                # has ended is not a live session and must not keep its folder
                # dispatchable for the half hour the tombstone survives. The
                # quiet-live rows promoted into this same bucket carry
                # `alive` and do still count.
                if name == "finished" and not row.get("alive"):
                    continue
                cwd = row.get("cwd") or ""
                if cwd:
                    roots.add(dispatch.normalise_root(cwd))
                    # A session in a card folder is plainly working in its
                    # checkout too, and the checkout is what a card it files
                    # names (`worktrees.checkout_root`).
                    home = worktrees.checkout_root(cwd)
                    if home != cwd:
                        roots.add(dispatch.normalise_root(home))
        roots.discard("")
        # Intersected with the enrolment ledger, at the instant of dispatch:
        # this is the "an unenrolled project cannot make Dark Army open a terminal"
        # property, and a snapshot read would let a card armed while enrolled
        # start after un-enrolment.
        return {r for r in roots if enrollment.root_enrolled(r)}

    #: The last agents snapshot, kept only so `_known_project_roots` can see the
    #: fleet's cwds. Deliberately not a second source of truth for anything else.
    _agents_snapshot_cache: dict = {}

    def _session_place(self, session_id: str) -> tuple:
        """Where a session is working: `(cwd, project)`, either possibly `""`.

        **The agents snapshot first, and that order is unchanged.**
        `_session_states` is built from hook payloads, and it used to carry no
        folder at all — `session_start` recorded state, last_event, project,
        tool_name and prompted, and nothing wrote a cwd — so the card path that
        read `state["cwd"]` got `""` every time, filed every agent-written card
        with an empty root, and `dispatch.guard` then refused each of them with
        "this card does not say which folder to work in". The card *looked*
        correct until somebody tried to start it. Since 30 Aug 2026 the key
        exists, stored from hook messages the enrolment gate has admitted, so
        the state fallback below can now answer; the snapshot still goes first,
        because its `project` is the workspace name every other surface uses.

        The snapshot is where a session's folder actually lives: `cwd` is
        usually on the row (it is what `_known_project_roots` unions in) and
        `project` is the VS Code workspace `workspace.py` resolved, which is the
        name every other surface uses. `_session_states` stays as the fallback
        for the project name, since a session can be in the state map a tick
        before it is in a snapshot.

        **A row can carry the project and no cwd, and the same session can hold
        more than one row.** A `finished` tombstone now carries the folder when
        it was known at the moment the run ended (`_record_finished` freezes it
        from this very method) but can still be cwd-less for a session that was
        never in a snapshot, and
        several live rows were measured with `cwd: ""` while their `project` was
        resolved — so returning on the *first* row that has either field meant a
        project-only row shadowed a row that knew the folder. That is not a
        cosmetic loss: `root` is what `dispatch.guard` reads, so twelve
        agent-written cards were filed with an empty root and could never be
        started. The rows are therefore scanned to the end, the cwd is taken
        from the first row that actually has one, and a project-only row is kept
        only as the name.
        """
        cwd = ""
        project = ""
        for group in (self._agents_snapshot_cache or {}).values():
            if not isinstance(group, list):
                continue
            for row in group:
                if not isinstance(row, dict):
                    continue
                if row.get("session_id") != session_id:
                    continue
                if not cwd:
                    cwd = str(row.get("cwd") or "")
                if not project:
                    project = str(row.get("project") or "")
        if cwd or project:
            return cwd, project
        state = self._session_states.get(session_id) or {}
        cwd = str(state.get("cwd") or "")
        project = str(state.get("project") or "")
        if cwd or project:
            return cwd, project
        # Grok under the shared leader: the channel's getcwd() is the leader's
        # folder, and this session is often missing from the snapshot. The
        # on-disk session directory is the folder the user actually opened.
        grok_cwd = grok_roster.folder_for_session(session_id)
        if grok_cwd:
            return grok_cwd, Path(grok_cwd).name
        return "", ""

    @staticmethod
    def _root_for_project_name(name: str, labels: list, known: set) -> str:
        """The folder a project *name* stands for, or `""`.

        The name may be the workspace label or the folder's own name; both are
        things a person would say, and both resolve to one root. Matched against
        what Dark Army can currently see rather than taken on trust — a caller cannot
        file work against a project it has nothing to do with by inventing a
        name.
        """
        if not name:
            return ""
        for entry in labels or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                if entry.get("root"):
                    return str(entry["root"])
        for candidate in known or ():
            if os.path.basename(candidate) == name:
                return candidate
        return ""

    async def _roster_verdict(self, workflow, root: str):
        """`(kept, dropped)` for a workflow judged against the project's own
        roster, or `None` where nothing may be judged.

        The one place a stage name meets a project's real helpers. Its two
        callers read the same answer for opposite ends: `update_card` refuses
        a changed box (`_off_roster_refusal`), and `create_card` drops the
        strangers, because the composer has no box to fix them in.

        Four deliberate silences, and two of them are the security case.

        An **empty** workflow says nothing — clearing the box is always
        allowed. An **unreadable or empty roster** refuses nothing: a project
        with no `.claude/agents` directory must not have its board locked, and
        `read_roster` never raises, so `[]` covers both "no directory" and
        "nothing in it".

        An **empty root** judges nothing. A card that names no folder has no
        project roster to judge it against, and `read_roster("")` would merge
        `~/.claude/agents` with a *relative* `.claude/agents` resolved against
        the daemon's own working directory — so a rootless card would be
        refused, or not, by names living in `$HOME`. The known-roots test below
        already turns `""` away; this rung is kept explicit because it is the
        one that says *why* rather than merely *whether*.

        A root that is **not a known project root** judges nothing either.
        `root` arrives straight off the payload (`ApiServer._BOARD_FIELDS`
        carries it, and `board_update` is on both `LAN_ACTIONS` and
        `REMOTE_ACTIONS`), so without this a paired phone could aim the roster
        walk at any absolute path on the machine and read the refuse/accept
        split as a yes-no oracle for "does that directory declare an agent
        called X". `create_card` applies the same containment to a root it is
        *given*; this is that check moved to the one seam both write paths
        share.

        The roster read is a directory walk and goes to the executor, the
        shape `refine`/`prepare` already use. Board writes are rare and
        snapshot pushes never reach this.
        """
        stages = board.parse_stages(workflow)
        if not stages:
            return None
        if not root:
            return None
        loop = asyncio.get_running_loop()
        known = await loop.run_in_executor(None, self._known_project_roots)
        if dispatch.normalise_root(root) not in known:
            return None
        roster = await loop.run_in_executor(
            None, card_prepare.read_roster, dispatch.normalise_root(root))
        if not roster:
            return None
        return board_workflow.keep_known_stages(
            stages, [name for name, _desc in roster])

    async def _off_roster_refusal(self, workflow, root: str) -> str:
        """`OFF_ROSTER_STAGE_REFUSAL` for a workflow naming a helper this
        project does not declare, `""` when there is nothing to say.

        The *editing* seam, `update_card`'s alone. Refusing there is the whole
        point: the card editor has an Expected specialists box, so a refusal is
        the last moment somebody can still fix a spelling, and a stored
        off-roster name is a stranger's face on the card with no job under it
        and no route back to whoever typed it. `create_card` has no such box
        behind it and drops instead — see the note there.
        """
        verdict = await self._roster_verdict(workflow, root)
        if verdict is None:
            return ""
        _kept, dropped = verdict
        if not dropped:
            return ""
        return _D.OFF_ROSTER_STAGE_REFUSAL % ", ".join(dropped)

    async def create_card(self, fields: dict, *, _author_guard=None) -> tuple:
        """Write a card. `(card_or_None, detail)`."""
        if self._board is None:
            return None, "the board is not open"
        fields = dict(fields or {})
        loop = asyncio.get_running_loop()
        root = str(fields.get("root") or "")
        needs_resolution = not board.parse_stages(fields.get("workflow"))
        known = set()
        if root or needs_resolution:
            known = await loop.run_in_executor(None, self._known_project_roots)
        if root:
            # A root is checked when it is *given*, so a nonsense folder is
            # refused at the point somebody could still fix it rather than at
            # the point they press Start. Dispatch re-checks it anyway: a window
            # can close between writing a card and starting it.
            if dispatch.normalise_root(root) not in known:
                return None, "that folder is not a project Dark Army can see"
            fields["root"] = dispatch.normalise_root(root)
        if needs_resolution:
            try:
                # The card's own project's real helpers, read on the executor
                # (a directory walk). A plan document is another repository's
                # prose as often as it is this one's, so a header naming
                # `sf-planner` must not stock this card with it.
                #
                # `read_roster` returns `[]` rather than raising, and `resolve`
                # guards its filter with `if roster:` — so a project with no
                # `.claude/agents` directory takes the **unfiltered** path and
                # is seeded exactly as it was before this filter existed. That
                # is deliberate and matches the refusal seam
                # (`_off_roster_refusal`), which also judges nothing against an
                # empty roster: a missing agents directory must not quietly
                # empty every card's expectations. The `except` below is for a
                # genuinely raising resolve, not for an unreadable roster.
                #
                # A card naming no folder is not read at all: `read_roster("")`
                # merges `~/.claude/agents` with a `.claude/agents` relative to
                # the daemon's own cwd, so a rootless card would be filtered
                # against `$HOME`'s helpers rather than a project's. That is
                # `_off_roster_refusal`'s empty-root rung, on the seeding side.
                card_root = str(fields.get("root") or "")
                roster = [
                    name for name, _desc in await loop.run_in_executor(
                        None, card_prepare.read_roster, card_root)
                ] if card_root else []
                fields["workflow"] = await loop.run_in_executor(
                    None, board_workflow.resolve, fields, known, roster)
            except Exception:
                # No workflow is the honest degradation: the expectation is
                # evidence, and a card that cannot be written because Dark Army could
                # not read a plan is an unanswered request over a nicety.
                logger.warning("could not resolve stages for a new card",
                               exc_info=True)
                fields["workflow"] = []
        else:
            # A create carries a workflow **nobody typed**. Neither composer
            # has an Expected specialists box: the faces come from Prepare,
            # computed against whichever project the picker held when it ran,
            # and the picker can move afterwards — Prepare's own folder
            # suggestion moves it. Refusing here was therefore a dead end, a
            # form with no box to correct and no way to save, which is exactly
            # what a phone hit against another project's helpers. So the
            # strangers are dropped, `_handle_board_card_request`'s rule and
            # the plan-seeded branch's rule above; the refusal stays on
            # `update_card`, the one write a person types into a box.
            verdict = await self._roster_verdict(
                fields.get("workflow"), str(fields.get("root") or ""))
            if verdict is not None:
                kept, _dropped = verdict
                fields["workflow"] = kept
        if _author_guard is not None and not _author_guard():
            return None, "Dark Army could not tell which session asked"
        card, detail = await self._board_call("create", fields)
        if card is not None:
            await self._publish_board()
        return card, detail

    async def promote_card(self, card_id: str) -> tuple:
        """Turn a Done scout into a Prep build card carrying its report.
        `(new_card_or_None, detail)`.

        Creates a card and dispatches nothing. Refused in words when the
        card is not a Done scout, has no report, or a card in **any**
        column already starts with the same `From report:` line — a
        promoted card that has been started, or finished, is still the
        promotion. The new card copies title (a leading `Scout:`
        stripped), summary, area, root, project and tool; its `kind` is
        `''` and its prompt leads with `From report: <path>`. An over-long
        composed prompt is clamped from the tail so the lead line is never
        cut. Goes through `create_card`, so a scout whose project has no
        editor window is refused in the composer's own words.

        The scan and the create sit under `_board_write_lock`, the lock
        `update_card` takes for the same check-then-write shape: two
        presses landing together (the phone's queue replays one, the panel
        sends another) would each find no twin and write two cards.
        """
        if self._board is None:
            return None, "the board is not open"
        card = await self._board_call("get", card_id)
        if card is None:
            return None, "no such card"
        if str(card.get("kind") or "") != board.KIND_SCOUT:
            return None, "only a scout card can be promoted"
        if str(card.get("column_name") or "") != "done":
            return None, "finish the scout first — Promote takes a Done scout"
        report_path = str(card.get("report_path") or "").strip()
        if not report_path:
            return None, "this scout has no report attached — nothing to promote"
        lead = f"From report: {report_path}"
        # The report's answer block, read off disk at the press on the
        # executor and never cached — **before** `_board_write_lock`, so a
        # slow or hostile file can hold up this press alone and never every
        # board write. `{}` (today's headerless card) for a report with no
        # block, or one that no longer passes the attach's containment check.
        loop = asyncio.get_running_loop()
        header = await loop.run_in_executor(
            None, self._promote_header, str(card.get("root") or ""),
            report_path)
        async with self._board_write_lock:
            existing = await self._board_call("cards") or []
            for other in existing:
                if str(other.get("prompt") or "").startswith(lead):
                    title = str(other.get("title") or "")
                    return None, f"already promoted — see «{title}»"
            return await self.create_card(
                self._promoted_fields(card, lead, header))

    @staticmethod
    def _promote_header(root: str, report_path: str) -> dict:
        """The answer block of the report a Done scout attached, or `{}`.

        **Blocking** — executor only. The stored path is re-checked with
        the attach's own `_plan_path_refusal` (inside the card's root after
        realpath, `.md`, a regular file, bounded), because the file may have
        been replaced since the attach — by a symlink out of the project or
        a FIFO. Any refusal reads nothing. `scout_report.read_header` then
        opens non-blocking and reads only a regular file, which closes the
        gap between that check and the open."""
        resolved, refusal = BoardVerbsMixin._plan_path_refusal(root, report_path)
        if refusal or not resolved:
            return {}
        return scout_report.read_header(resolved)

    @staticmethod
    def _promoted_fields(card: dict, lead: str, header: dict = None) -> dict:
        """The Prep build card a Done scout becomes — `promote_card`'s
        composition, pure.

        With no `header` (or one with no verdict) the card is composed
        exactly as before the scout report had an answer block. With one,
        the title is the first follow-up's (else the scout's own, `Scout:`
        stripped), the summary is the verdict, and the notes carry the
        recommendation, the question and every follow-up after the lead
        line. Either way the prompt is clamped from the tail, so the lead
        line — which the twin check reads — is never cut."""
        title = str(card.get("title") or "")
        stripped = title
        if title.lower().startswith("scout:"):
            rest = title[6:].lstrip()
            if rest:
                stripped = rest
        close_note = str(card.get("close_note") or "").strip()
        original = str(card.get("prompt") or "").strip()
        summary = str(card.get("summary") or "")
        parts = [lead]
        verdict = str((header or {}).get("verdict") or "").strip()
        if verdict:
            follow_ups = [f for f in header.get("follow_ups") or []
                          if isinstance(f, dict)]
            first = str(follow_ups[0].get("title") or "").strip() \
                if follow_ups else ""
            if first:
                stripped = first
            stripped = stripped[:board.MAX_TITLE_CHARS]
            summary = verdict[:board.MAX_SUMMARY_CHARS]
            recommendation = str(header.get("recommendation") or "").strip()
            confidence = str(header.get("confidence") or "").strip()
            if recommendation:
                line = f"Recommendation: {recommendation}"
                if confidence:
                    line += f" ({confidence} confidence)"
                parts.append(line)
            elif confidence:
                parts.append(f"Confidence: {confidence}")
            question = str(header.get("question") or "").strip()
            if question:
                parts.append(f"Question: {question}")
            if follow_ups:
                rows = []
                for item in follow_ups:
                    name = str(item.get("title") or "").strip()
                    words = str(item.get("summary") or "").strip()
                    rows.append(f"- {name} — {words}" if words else f"- {name}")
                parts.append("Follow-ups:\n" + "\n".join(rows))
        if close_note:
            parts.append(close_note)
        if original:
            parts.append(original)
        prompt = "\n\n".join(parts)
        if len(prompt) > board.MAX_PROMPT_CHARS:
            prompt = prompt[:board.MAX_PROMPT_CHARS]
        fields = {
            "title": stripped,
            "summary": summary,
            "area": str(card.get("area") or ""),
            "root": str(card.get("root") or ""),
            "project": str(card.get("project") or ""),
            "tool": str(card.get("tool") or ""),
            "kind": "",
            "column_name": "prep",
            "prompt": prompt,
        }
        return fields

    async def create_card_and_refine(self, fields: dict) -> tuple:
        """The composer's one press: write a card, then refine it.

        `(card_or_None, detail, refine_ok, refine_detail)`. The create is
        `create_card` unchanged; the refinement is `refine_card` — never
        `_refine_card_locked` — so the `board_dispatch` preference,
        `_dispatch_lock`, `refine_guard`, the cooldown and the shared
        in-flight budget all apply exactly as they do to a Refine press on
        the board. A refused refinement never undoes the create: the card is
        already in Prep and the person asked for it.

        The refusal lands on the new card's `dispatch_error` because both
        surfaces already draw that line in orange, and the composer that made
        the press has dismissed by the time anything could be shown there.
        A refusal from `refine_card` *before* the lock (the preference off,
        the board closed) never reaches the store on its own, so this write
        is what makes the phone see anything at all.
        """
        card, detail = await self.create_card(fields)
        if card is None:
            return None, detail, False, ""
        if detail == board.ALREADY_CREATED:
            return card, detail, False, ""
        ok, refine_detail = await self.refine_card(card["id"])
        if not ok:
            await self._board_call("update", card["id"],
                                   {"dispatch_error": refine_detail})
            await self._publish_board()
        return card, detail, ok, refine_detail

    async def _plan_gate_refusal(self, card: dict, *, replay: bool = False) -> str:
        """Why this card may not enter In progress unconfirmed, or `""`.

        The plan gate, and it is a **confirmation, not a wall**: every caller
        takes `allow_unplanned`, and the refusal's own words offer the way
        through. It fires only when *all* of: the card's `plan_path` is empty
        — or names a file that is no longer there (`isfile` on the executor,
        so the loop never stats) — **and** the card carries no `session_id`
        and no `link_state`. The session/link exemption is deliberate: a card
        whose assistant already ran (or is bound) being moved is *tracking*,
        not admitting unplanned work. It also keeps the daemon's own writes
        structurally outside the gate — `bind_session` and `_wrap_up_for_done`
        go through the store, and this lives only in the surface-facing verbs
        (`update_card`, `reorder_card`, `dispatch_card`).

        Keyed on the **field**, not the column: an old unplanned Backlog card
        gets the same confirmation as a Prep card somebody is strong-arming —
        one rule, stated once, rather than a grandfather clause that quietly
        exempts most of the board from the thing the gate is for. The cost is
        one extra confirmed press on pre-Prep cards, and it decays to zero.

        The destination test (`in_progress`, and *arriving* rather than
        already there) lives at the call sites, which each know their own
        before/after shape.

        `replay` (keyword-only, so the positional callers cannot pass it by
        accident) says the caller is the queue's drain. **A queued card is a
        confirmed card**: it could only have entered the queue by passing
        this gate, or being confirmed past it, at the moment of the press, so
        asking either confirmation again is asking a question the person
        already answered — and the drain answering "no" on their behalf was
        the bug. On a replay the empty-`plan_path` rung and the changed-plan
        rung answer `""`; the one rung that survives is a `plan_path` naming
        a file that has gone, because `dispatch.start_prompt` would otherwise
        build `/ship implement <missing path>` and hand the assistant nothing.
        """
        card = card or {}
        if str(card.get("kind") or "") == board.KIND_SCOUT:
            return ""
        if str(card.get("session_id") or ""):
            return ""
        if str(card.get("link_state") or ""):
            return ""
        # `PLAN_GATE_REFUSAL`, never an inline string: the panel matches this
        # refusal's opening words to raise the confirmation, so a second copy
        # here would be two wordings that can drift apart — and the one that
        # drifts turns the gate into a wall.
        refusal = _D.PLAN_GATE_REFUSAL
        plan = str(card.get("plan_path") or "")
        if not plan:
            # No plan is the first confirmation, and a replay has it already.
            return "" if replay else refusal
        loop = asyncio.get_running_loop()
        exists, digest = await loop.run_in_executor(None, _plan_state, plan)
        if not exists:
            # A path to a file that is gone is *worse* than no plan — the card
            # claims a stage that cannot be read — so it gets the same gate,
            # **replay included**: this rung sits above the replay
            # short-circuit on purpose, because there is nothing to hand the
            # assistant.
            return refusal
        if replay:
            # The changed-plan rung is the second confirmation; a queued card
            # was confirmed past it at the press, or its plan matched then.
            return ""
        approved = str(card.get("plan_approved") or "")
        # **An empty approval never gates.** Approval is opt-in: every card on
        # every board that existed before v15 carries `''`, and a rung that
        # fired on absence would make the whole board unstartable on upgrade.
        # A *non-empty* approval that no longer matches the file is the one
        # thing this rung is for, and it gets its own words — the confirmation
        # it raises says "start with a changed plan", not "start unplanned".
        if approved and approved != digest:
            return _D.PLAN_CHANGED_REFUSAL
        return ""

    async def update_card(self, card_id: str, fields: dict,
                          allow_unplanned: bool = False) -> tuple:
        """Write a card's fields. `(card_or_None, detail)`.

        The pre-read is not decoration: `_after_board_write` has to be able to
        tell a card *arriving* in Done from a save of a card already sitting
        there, and only the row as it was a moment before the write says which.
        `_board_write_lock` spans the read and the write and nothing else — two
        overlapping moves into Done would otherwise both read a non-`done`
        `before` and both type a `/clear`, which is exactly the race
        `_dispatch_lock` exists for; and holding it across the wrap-up's VS Code
        round-trip would stall every board edit behind one unresponsive
        terminal.

        The plan gate rides inside the same lock, on the same pre-read, and
        only on an *arrival* in In progress — a save of a card already sitting
        there must not be refused over a field the save is not touching.

        The Expected specialists check is deliberately **outside** the lock, on
        a pre-read of its own: it walks a project's `.claude/agents` directory,
        and a root on an unresponsive mount would otherwise stall every board
        edit on the machine behind one save — the same reason the wrap-up's VS
        Code round-trip is not held either. Its pre-read may be a moment older
        than `before`; that is harmless, because the check compares the *stage
        names* this save carries against the ones already stored and neither
        the store nor any other write path re-reads its answer.
        """
        if self._board is None:
            return None, "the board is not open"
        # The Expected specialists box, judged only where this save is actually
        # **changing** it. A card that already holds an off-roster name stays
        # editable in every other field and heals the moment somebody fixes the
        # box — a refusal on an unchanged value would lock the one card that
        # most needs a person.
        if "workflow" in (fields or {}):
            stored = await self._board_call("get", card_id) or {}
            wanted = board.parse_stages((fields or {}).get("workflow"))
            if wanted != board.parse_stages(stored.get("workflow")):
                # Against the root this save is *setting*, not the stored one:
                # the card editor sends `root` and `workflow` in one payload, so
                # a move plus an edit would otherwise be judged against the
                # project the card is leaving.
                root = str(
                    (fields or {}).get("root") or stored.get("root") or "")
                refusal = await self._off_roster_refusal(wanted, root)
                if refusal:
                    return None, refusal
        async with self._board_write_lock:
            before = await self._board_call("get", card_id) or {}
            dest = str((fields or {}).get("column_name") or "")
            refusal = self._merge_move_refusal(before, dest)
            if refusal:
                return None, refusal
            if (not allow_unplanned and dest == "in_progress"
                    and str(before.get("column_name") or "") != "in_progress"):
                refusal = await self._plan_gate_refusal(before)
                if refusal:
                    return None, refusal
            card, detail = await self._board_call(
                "update", card_id, dict(fields or {}))
        if card is not None:
            await self._publish_board()
            await self._after_board_write(before, card)
        return card, detail

    async def _after_board_write(self, before: dict, after: dict) -> None:
        """The one place the card → session direction lives.

        A seam rather than two inline calls, because `update_card` and
        `reorder_card` are two doors onto one `BoardStore.update` — the panel
        uses the first for a drop onto a column and the second for a drop into
        the gap between two cards — and a hook written into only one of them is
        a feature that works for half the drags. A third door added later
        inherits this rather than repeating it, which is the lesson
        `BoardCardView.move` had to learn twice about the Backlog unlink.

        Called **only when the write landed** (`after` is a real card): acting
        on a refused update would reach into a session over a change that never
        happened.
        """
        # A human drag into Done, in the diary. Both drag verbs land here and
        # `close_card_by_session` never does, so an agent's close and a
        # person's drag are each exactly one line.
        if (str(after.get("column_name") or "") == "done"
                and str(before.get("column_name") or "") != "done"):
            self._log_card_event(after, "card_done", closed_by="user")
        await self._wrap_up_for_done(before, after)
        # A Done arrival whose session is already gone frees the card's own
        # worktree now; one still live is released at the reconcile's
        # `mark_ended` seam, once its shell has left the folder.
        if (str(after.get("column_name") or "") == "done"
                and str(before.get("column_name") or "") != "done"
                and str(after.get("worktree_path") or "")
                and str(after.get("link_state") or "") not in (
                    "live", "dispatching")):
            self._consider_worktree_release(after)
            await self._kick_worktree_releases()

    async def _wrap_up_for_done(self, before: dict, after: dict) -> None:
        """A card arriving in Done closes the live session bound to it.

        The return leg of `_reconcile_board`: that watches the session and
        updates the card, this watches the card and closes the terminal. The
        wrap-up (`/clear`) half is gone — a refused close leaves the tab
        alone, with one log line and no orange note.
        """
        if not self.board_close_terminal_enabled:
            return
        # An arrival, not an edit. Re-saving a card that is already Done, or
        # reordering within the Done column, must not fire a fresh close.
        if str(before.get("column_name") or "") == "done":
            return
        if str(after.get("column_name") or "") != "done":
            return
        # `link_state == "live"` is the whole gate, and it is silent — no note
        # is written. `""` (never dispatched), `"dispatching"` (nothing bound
        # yet) and `"ended"` (the session is already gone) all mean there is
        # nothing to close, and an orange complaint on a hand-written card
        # filed straight to Done would be Dark Army objecting to work it was never
        # asked to do. Returning here also leaves a genuine pre-existing
        # `dispatch_error` on such a card alone.
        #
        # Deliberately *not* `can_type`: that is False for a `running` session,
        # so it would skip exactly the live sessions this is for, and it is a
        # snapshot field rather than a callable.
        if str(after.get("link_state") or "") != "live":
            return
        sid = str(after.get("session_id") or "")
        if not sid:
            return
        # Codex belongs with the silent rungs above. Typing at Codex is not a
        # failure Dark Army had on a bad day, it is a capability that structurally
        # does not exist (`codex_rollouts`: reply, typing and wrap up are
        # unsupported).
        if str(after.get("tool") or "") == "codex" \
                or self._session_provider(sid) == "codex":
            return
        cid = str(after.get("id") or "")
        if sid in self._prompts_by_session():
            logger.info("card %s: not closing %s, a permission prompt is open",
                        cid[:8], sid[:12])
            return
        ok, detail = await self._close_session_terminal(sid)
        if ok:
            if str(after.get("dispatch_error") or ""):
                await self._board_call("update", cid, {"dispatch_error": ""})
                await self._publish_board()
            logger.info("closed the terminal of %s for card %s moved to done",
                        sid[:12], cid[:8])
            return
        logger.info("card %s: terminal close refused (%s)", cid[:8], detail)

    async def _batch_session_still_needed(self, card: dict, rsid: str) -> bool:
        """Whether another card is still being refined by this card's batch
        planning session. `False` for a single refinement (no `batch_id`),
        so its delete closes the terminal exactly as before.

        One session plans every card of a batch; deleting one of them
        cancels that card, not the others' interviews. "Still being
        refined" is `refine_state` `live` or `dispatching` — a sibling whose
        plan already landed keeps `refine_session_id` as a record
        (`attach_plan`) but no longer needs the terminal.
        """
        if not str(card.get("batch_id") or ""):
            return False
        cid = str(card.get("id") or "")
        others = await self._board_call("by_refine_session", rsid) or []
        return any(str(c.get("id") or "") != cid
                   and str(c.get("refine_state") or "") in ("live", "dispatching")
                   for c in others)

    async def _close_for_deleted_card(
            self, card: dict, shell_pid: Optional[int] = None) -> bool:
        """Close the terminal of a session whose card was just deleted.

        Delete means the work is cancelled, so the terminal has no reason to
        survive it — including a card in Done. Unconditional:
        `board_close_terminal` is not consulted — the armed label
        ("Delete & close terminal?") is the consent, standing in for the
        preference. Done is not exempt as of 2026-08-24: the
        declare_done-mid-report cost was shown and the delete gesture
        outranks it; there is no quiet-session check, delay, or preference.
        A refused close is a log line and the tab left alone; there is no
        card to write a `dispatch_error` onto, and typing `/clear` would
        destroy the transcript while failing to tidy the screen.

        A still-dispatching card has no `session_id` yet; when Dark Army still
        holds the spawn-shell receipt, the terminal is closed by that pid.

        Returns whether every close it attempted landed (True with nothing
        to close) — what the worktree release reads as "the shell is gone".
        """
        closed_all = True
        candidates = []
        if str(card.get("link_state") or "") == "live":
            sid = str(card.get("session_id") or "")
            if sid:
                candidates.append(sid)
        cid = str(card.get("id") or "")
        if str(card.get("refine_state") or "") == "live":
            rsid = str(card.get("refine_session_id") or "")
            if rsid and not await self._batch_session_still_needed(card, rsid):
                candidates.append(rsid)
        linked = str(card.get("session_id") or "")
        for sid in candidates:
            # Codex is skipped here, not because `_close_session_terminal`
            # cannot see a pid (the panel's close-terminal button now has a
            # third, explicit_resume-only rung), but because this route does
            # not close Codex tabs. `tool == "codex"` names the linked
            # session's assistant — skip it only for that sid. A Codex
            # refine session is skipped by `_session_provider(sid) ==
            # "codex"` for the same structural reason (no pid Dark Army may
            # control), not because Refine is secretly Claude. The
            # provider check is uniform on both candidates.
            if (sid == linked and str(card.get("tool") or "") == "codex") \
                    or self._session_provider(sid) == "codex":
                continue
            ok, detail = await self._close_session_terminal(sid)
            if ok:
                logger.info("closed the terminal of %s for deleted card %s",
                            sid[:12], cid[:8])
            else:
                closed_all = False
                logger.info("deleted card %s: left %s alone (%s)",
                            cid[:8], sid[:12], detail)
        if (str(card.get("link_state") or "") == "dispatching"
                and shell_pid
                and str(card.get("tool") or "") != "codex"):
            ok, detail = await self._close_terminal_by_pid(shell_pid)
            if ok:
                logger.info("closed the terminal of pid %s for deleted card %s",
                            shell_pid, cid[:8])
            else:
                closed_all = False
                logger.info("deleted card %s: left pid %s alone (%s)",
                            cid[:8], shell_pid, detail)
        return closed_all

    async def _finish_card_for_closed_session(self, session_id: str) -> None:
        """A person closed this session's terminal, so the card it was
        dispatched from is finished. Best-effort and silent.

        It never raises into the close and never changes the close's answer:
        the tab is already gone, and turning a landed close into a refusal
        would tell the presser to press again at a terminal that no longer
        exists. `_wrap_up_for_done`'s discipline, one rung on.

        Three exclusions are **inherited** from `close_card_by_session`
        rather than re-implemented here, which is the point of reusing it:

        - **A refining session is invisible to it.** `by_session` matches the
          `session_id` column; a card whose plan is being written is held
          under `refine_session_id` / `refine_state`, so the lookup is `[]`.
        - **The card-arrives-in-Done hook cannot fire**, so this cannot chase
          `_wrap_up_for_done` around in a loop. Two independent facts, not a
          flag: `declare_done` never enters `BoardStore.update`, and that verb
          never calls `update_card` / `reorder_card` — so `_after_board_write`
          is structurally unreachable from here.
        - **A card already in Done is filtered out**, so the ordinary case —
          the agent called `dark_army_close_card` during its own turn and the person
          then closed the tab — is a no-op reading "already done".

        Codex is deliberately *not* excluded, unlike in `_wrap_up_for_done`
        and `_close_for_deleted_card`. Those skip it because Dark Army cannot type
        at Codex; this writes a board row and types nothing.
        """
        if self._board is None:
            return
        card, detail = await self.close_card_by_session(
            session_id, _D.CLOSE_TERMINAL_DONE_NOTE, by="terminal_close")
        if card is None:
            logger.info("close terminal %s: finished no card (%s)",
                        session_id[:8], detail)
            return
        # The person who pressed Close terminal has already read what the
        # session left on screen — that press *is* their review. Without
        # this the card wore "FINISHED · REVIEW" and sat in Needs you on
        # both surfaces waiting for the same person to press Reviewed on
        # their own close. `mark_reviewed`'s WHERE still decides: a card
        # reopened in the meantime is left alone, and a refusal is one log
        # line, never a changed answer to the close.
        reviewed, why = await self.review_card(card["id"])
        if reviewed is None:
            logger.info("close terminal %s: card %s not reviewed (%s)",
                        session_id[:8], str(card.get("id") or "")[:8], why)

    async def close_card_by_session(self, session_id: str, note: str,
                                    *, by: str = "") -> tuple:
        """Close the one card this session is working on. `(card_or_None, detail)`.

        The card is resolved from the session and never named by the caller —
        that is the whole scope restriction `_handle_board_close_request`
        argues for, and it lives here so no second route can skip it.

        Ambiguity **fails closed**, exactly as `_channel_session`'s own
        pid-to-session rule does: a session somehow bound to two live cards
        cannot be guessed at, so it is refused and the human closes it on the
        board. Cards already in Done are not counted, so a session that closed
        one card and is still bound to it is not called ambiguous the next time
        it is asked about.

        **A second call reads as idempotent, not as a broken binding.** Once
        Done cards are filtered out, a session that has already closed its card
        matches nothing — and "no card on Dark Army's board names this session" would
        send the model looking for a scope or attribution failure that is not
        there, and quite possibly reporting one. A `/ship` retry, or an agent
        re-confirming, is told the card is already done, which is the truth.

        **One card this can reach that a human moved out of the way.** `update`
        does not clear `session_id` when a card leaves In progress, so a card a
        human pressed Back on sits in Backlog still bound to its session, and
        that session can later pull it into Done. That is pre-existing binding
        behaviour and it is deliberately left alone — the binding is a record of
        what ran, and Reopen is the undo — but it is the one route by which a
        close reaches a card somebody had deliberately set aside.

        `by` names the actor for the **diary line alone** — the `closed_by`
        detail `event_log.sentence` reads, which has never been the store
        column. The store's own `closed_by` is written by `declare_done` and
        is always the calling session's id; `declare_done` stays its single
        writer. The two spellings of the word differ in meaning at exactly
        this one call site, which is why it is said out loud here.
        """
        # Deliberately **not** routed through the card-arrives-in-Done hook,
        # and the exclusion is structural in two independent ways rather than a
        # flag anybody can forget: `declare_done` is its own single UPDATE that
        # never enters `BoardStore.update`, and this method never calls
        # `update_card`. An agent closing its own card is doing it *during its
        # own turn* — exactly what `/ship` Phase 7b does — and typing `/clear`
        # there would destroy the context mid-report.
        if self._board is None:
            return None, "the board is not open"
        if not session_id:
            return None, "Dark Army could not tell which session asked"
        cards = await self._board_call("by_session", session_id) or []
        # A batch session is bound to every card it has worked; the one it
        # is on now is the one it has not moved past (`_narrow_batch_open`).
        open_cards = self._narrow_batch_open(
            [c for c in cards if c.get("column_name") != "done"])
        if not open_cards:
            if cards:
                return None, "that card is already done — nothing to do"
            return None, "no card on Dark Army's board names this session"
        if len(open_cards) > 1:
            return None, ("this session is on more than one card — "
                          "close it on the board")
        card, detail = await self._board_call(
            "declare_done", open_cards[0]["id"], session_id, note)
        if card is not None:
            await self._publish_board()
            # Logged here and only here: this verb never enters
            # `_after_board_write`, so an agent close is exactly one line.
            self._log_card_event(
                card, "card_done", session_id=session_id,
                closed_by=by or (self._nicknames_shown.get(session_id, "")
                                 or "its agent"),
                note=str(note or "")[:200])
        return card, detail

    @staticmethod
    def _narrow_batch_open(open_cards: list) -> list:
        """A batch session's open cards, minus the members it moved past.
        Pure.

        A batch-implement session stays bound to every card it has worked:
        one left unclosed by `dark_army_next_card` is still In progress,
        still names the session, and is `ended`. The card the session is on
        now is the one that is not — the positional rule
        `docs/channel-tools.md` states. A card with no batch mark is never
        dropped, so a single-card session reaches this with one card and
        leaves with it, and two unmarked cards still fail closed.
        """
        if len(open_cards) < 2:
            return open_cards
        return [c for c in open_cards
                if not (str(c.get("batch_id") or "")
                        and str(c.get("link_state") or "") == "ended")]

    async def flag_manual_by_session(self, session_id: str, steps: str,
                                     path: str = "") -> tuple:
        """Flag the one card this session is working on. `(card_or_None, detail)`.

        `close_card_by_session`'s resolution verbatim, and deliberately so: the
        two verbs are the same session saying opposite things about the same
        card, so a difference in *which* card they reach would be a bug with no
        argument behind it. The card is resolved from the session and never
        named by the caller; ambiguity fails closed; a session whose only card
        is already in Done is told so rather than sent looking for an
        attribution failure that is not there.

        This moves no card and closes nothing. It is a note for a person,
        recorded on the card the session is already the running author of.

        **The flag may follow the close** (v26): a card with an open check
        goes to Done, so with no open card the session's one Done card *it
        closed itself* (`closed_by == session_id`) is flagged instead. Two
        such cards are ambiguous and refused in words; any other Done card
        keeps the old refusal.

        ``path`` names the check file the session wrote. It is validated on
        the executor (`_manual_check_path_refusal`: inside the card's root,
        under its `manual-check/` folder, passing `manual_check.check`) and
        stored as its realpath; no path flags exactly as before.
        """
        if self._board is None:
            return None, "the board is not open"
        if not session_id:
            return None, "Dark Army could not tell which session asked"
        cards = await self._board_call("by_session", session_id) or []
        open_cards = self._narrow_batch_open(
            [c for c in cards if c.get("column_name") != "done"])
        if not open_cards:
            if not cards:
                return None, "no card on Dark Army's board names this session"
            closed = [c for c in cards
                      if c.get("column_name") == "done"
                      and str(c.get("closed_by") or "") == session_id]
            marks = {str(c.get("batch_id") or "") for c in closed}
            if len(closed) > 1 and len(marks) == 1 and "" not in marks:
                # One batch session closed several cards, in order: the
                # newest close is the card it has just finished, which is
                # the one a flag-after-close means.
                closed = [max(closed,
                              key=lambda c: float(c.get("done_at") or 0.0))]
            if len(closed) > 1:
                return None, ("this session closed more than one card — "
                              "say which on the board")
            if not closed:
                return None, ("that card is already done — reopen it if a "
                              "check is still outstanding")
            target = closed[0]
        elif len(open_cards) > 1:
            return None, ("this session is on more than one card — "
                          "say which on the board")
        else:
            target = open_cards[0]
        extra = ()
        if str(path or "").strip():
            loop = asyncio.get_running_loop()
            resolved, refusal = await loop.run_in_executor(
                None, self._manual_check_path_refusal,
                target.get("root") or "", path)
            if refusal:
                return None, refusal
            extra = (resolved,)
        card, detail = await self._board_call(
            "flag_manual", target["id"], session_id, steps, *extra)
        if card is not None:
            await self._publish_board()
            self._log_card_event(card, "card_manual", session_id=session_id,
                                 steps=str(steps or "")[:200])
        return card, detail

    async def clear_manual_check(self, card_id: str,
                                 expected_manual_steps=None) -> tuple:
        """A person saying they have done the hand-check. `(ok, detail)`.

        ``expected_manual_steps`` is the phone's echo of the steps it drew,
        `None` from the Mac's own press: `BoardStore.clear_manual` ANDs a
        present echo into its WHERE and leaves the statement alone otherwise.

        `unqueue_card`'s shape and `unqueue_card`'s argument:
        `manual_steps` is outside `ApiServer._BOARD_FIELDS`, so a surface may
        **clear** an outstanding check and may never raise one — a surface that
        could raise one could hang a chore on somebody else's card, and the
        badge would stop meaning "the session that did this work said so".

        Unarmed on the surface that offers it: the undo is that the session can
        say it again.
        """
        if self._board is None:
            return False, "the board is not open"
        # The echo rides only when it was sent: the Mac's own press makes
        # exactly the call it made before this argument existed.
        kwargs = {}
        if expected_manual_steps is not None:
            kwargs["expected_manual_steps"] = expected_manual_steps
        card, detail = await self._board_call("clear_manual", card_id, **kwargs)
        if card is None:
            return False, detail
        await self._publish_board()
        self._log_card_event(card, "card_manual_clear")
        await self._settle_handled_card(
            str(card.get("id") or ""),
            str(card.get("closed_by") or card.get("session_id") or ""))
        return True, "marked as checked"

    def _settle_checked_card(self, card_id: str) -> None:
        """Mark checked is the person's hand on this card, so the Needs you
        entry the card is *still* live under is acknowledged with it.

        Measured 20 Sep 2026: a card whose run had ended carried a hand-check
        too. `inbox_ack.card_kind_and_fp` ranks the ended run above the check,
        so the row on Needs you was the ended-run one (LOOK AT, the summary
        for its detail) and the check sat behind it. The person opened the
        card, pressed Mark checked, the steps cleared — and the row stayed,
        because the press touched the check alone and the ended-run entry
        under it had never been acknowledged. To the person that is the item
        they just dealt with coming back under the same word.

        Read after `_publish_board`, so `_board_state` already says what the
        card is live under with the check gone: `ended_work` where the run
        has ended, nothing where the assistant is still working (then there
        is nothing to hide). The store's own "hide until it changes" rule is
        unchanged — this is the same ack Dismiss would have written, made by
        the stronger gesture.
        """
        store = getattr(self, "_inbox_acks", None)
        if store is None or not card_id:
            return
        live = self._inbox_live("c:" + card_id)
        if live is None:
            return
        kind, fingerprint = live
        if kind not in inbox_ack.ACK_KINDS:
            return
        store.ack("c:" + card_id, kind, fingerprint)

    async def review_card(self, card_id: str, expected_closed_by=None,
                          expected_close_note=None) -> tuple:
        """A person acknowledging an assistant's close. `(card_or_None, detail)`.

        The two ``expected_*`` are the phone's echo of the close it drew,
        both `None` from the Mac's own press: `BoardStore.mark_reviewed`
        ANDs a present pair into its WHERE, refuses half a pair before any
        UPDATE, and leaves the statement alone otherwise.

        The verb behind the Reviewed press: it drops the "FINISHED · REVIEW"
        banner by stamping `reviewed_at`, which no surface can write as a field
        (`ApiServer._BOARD_FIELDS` excludes it — a Save must never silently
        acknowledge a review). Deliberately takes no `_board_write_lock` and
        never touches `_after_board_write`: it moves no column, so the wrap-up
        leg has no business here, and the race answer is the WHERE clause in
        `mark_reviewed` itself — `declare_done`'s pattern.
        """
        if self._board is None:
            return None, "the board is not open"
        # Each half rides only when it was sent — the store is what judges
        # a half pair — so the Mac's own press makes exactly the call it
        # made before these arguments existed.
        kwargs = {}
        if expected_closed_by is not None:
            kwargs["expected_closed_by"] = expected_closed_by
        if expected_close_note is not None:
            kwargs["expected_close_note"] = expected_close_note
        card, detail = await self._board_call("mark_reviewed", card_id, **kwargs)
        if card is not None:
            await self._publish_board()
            await self._settle_handled_card(
                str(card.get("id") or ""),
                str(card.get("closed_by") or card.get("session_id") or ""))
        return card, detail

    async def _settle_handled_card(self, card_id: str,
                                   session_id: str = "") -> None:
        """A person's hand on a card — Mark reviewed, or Acknowledge & close
        on its agent — clears what that card and its agent still have on
        Needs you, **exactly as Dismiss would**. Mark checked has done this
        for the card alone since 20 Sep 2026 (`_settle_checked_card`); the
        report of 25 Sep 2026 was the same trap one press over: a Done card
        with a hand-check attached, reviewed on the phone, its `manual_check`
        row still on Needs you until the person dismissed it separately.

        The same ack Dismiss writes, so the same "hide until it changes"
        rule: a new check, a new question or a new finished turn comes back.
        The agent's subject is settled only when it is a plain finished
        wait — a live question or a permission ask is the agent asking
        something a review does not answer, and is left on the list.
        Best-effort: never raises into the verb that called it."""
        try:
            self._settle_checked_card(card_id)
            store = getattr(self, "_inbox_acks", None)
            if store is not None and session_id:
                live = self._inbox_live("s:" + session_id)
                if live is not None and live[0] == "waiting":
                    store.ack("s:" + session_id, *live)
                    await self._settle_acknowledged_session(session_id, "waiting")
            await self._wake_surfaces()
        except Exception:  # pragma: no cover - a settle must never fail a press
            logger.debug("settle for card %s failed", card_id[:8], exc_info=True)

    async def _settle_closed_session(self, session_id: str) -> None:
        """Acknowledge & close by a person: every card the closed session
        was working on (live link or its own close) is settled as handled,
        even when the card was already in Done and the close finished
        nothing — the ordinary case, an agent that closed its own card."""
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        ids = [str(c.get("id") or "") for c in cards if isinstance(c, dict)
               and session_id in (str(c.get("session_id") or ""),
                                  str(c.get("closed_by") or ""))]
        for cid in [c for c in ids if c] or [""]:
            await self._settle_handled_card(cid, session_id)

    async def approve_card_plan(self, card_id: str, plan_path: str,
                                digest: str) -> tuple:
        """Record a person's approval of the plan version they just read.
        `(card_or_None, detail)`.

        The caller echoes back both the path and the digest the card read
        handed it, and this refuses unless *both* still describe the file on
        disk **now**. That echo is what makes this an approval of a version
        somebody actually saw rather than of whatever the file happens to say
        at the moment of the press: a client never computes a hash, and a
        client that guessed one would be refused by the same test.

        One executor hop does the containment check and the digest together —
        `_plan_path_refusal` stats and realpaths, `_plan_digest` reads and
        hashes, and neither may run on the loop.
        """
        if self._board is None:
            return None, "the board is not open"
        card = await self._board_call("get", card_id)
        if card is None:
            return None, "no such card"
        stored = str(card.get("plan_path") or "")
        if not stored:
            return None, "that card has no plan to approve"
        loop = asyncio.get_running_loop()

        def read_state():
            resolved, refusal = self._plan_path_refusal(
                str(card.get("root") or ""), stored)
            if refusal:
                return "", refusal
            return _plan_digest(resolved), ""

        current, refusal = await loop.run_in_executor(None, read_state)
        if refusal:
            return None, refusal
        if not current:
            return None, "that plan could not be read — open it again"
        asked = str(digest or "").strip()
        if asked != current or str(plan_path or "").strip() != stored:
            return None, ("the plan changed while you were reading it — "
                          "open it again")
        approved, detail = await self._board_call(
            "approve_plan", card_id, stored, current, time.time())
        if approved is None:
            return None, detail
        await self._publish_board()
        self._log_card_event(approved, "card_plan_approved")
        return approved, detail

    @staticmethod
    def _plan_path_refusal(root: str, path: str) -> tuple:
        """Validate a plan path against a card's own root. `(resolved, detail)`.

        **Blocking** (realpath and two stats) — executor only. The path is
        untrusted text arriving over the hook socket, which is
        `board_workflow`'s containment argument in reverse: there the card
        names a file Dark Army reads, here a session names a file Dark Army will hold up
        as somebody's plan. Both sides go through `dispatch.normalise_root`'s
        realpath, so a symlink written inside the project cannot walk out of
        it and `/tmp` vs `/private/tmp` cannot split a project in two.
        """
        base = dispatch.normalise_root(root)
        if not base:
            return "", "this card does not say which folder to work in"
        text = str(path or "").strip()
        if not text:
            return "", "a plan needs a path"
        candidate = text if os.path.isabs(os.path.expanduser(text)) \
            else os.path.join(base, text)
        resolved = os.path.realpath(os.path.expanduser(candidate))
        if resolved != base and not resolved.startswith(base + os.sep):
            return "", "the plan must be a file inside the card's own project"
        if not resolved.lower().endswith(".md"):
            return "", "a plan is a Markdown (.md) file"
        if not os.path.isfile(resolved):
            return "", "there is no file at that path"
        try:
            size = os.path.getsize(resolved)
        except OSError:
            return "", "there is no file at that path"
        if size > board_workflow.MAX_PLAN_BYTES:
            return "", "that file is too large to be a plan"
        return resolved, ""

    @staticmethod
    def _report_shape_refusal(root: str, resolved: str) -> str:
        """`""`, or the refusal for a scout report that is not in the shape.

        **Blocking** (reads the file) — executor only. Only a report whose
        resolved path sits under `<root>/scout/` is checked; anything else
        in the root (an older prose report under `docs/research/`) attaches
        as before. `resolved` is `_plan_path_refusal`'s realpath, and the
        root goes through the same `dispatch.normalise_root`, so the
        comparison is realpath against realpath. The folder's own name is
        compared without case: a macOS disk is case-insensitive, so
        `Scout/…` is the same folder and must not skip the check.
        """
        base = dispatch.normalise_root(root)
        if not base:
            return ""
        text = str(resolved or "")
        if not text.startswith(base + os.sep):
            return ""
        segment, sep, _rest = text[len(base) + len(os.sep):].partition(os.sep)
        if not sep or segment.casefold() != scout_report.FOLDER.casefold():
            return ""
        # scout_report's own bounded read: non-blocking, no final symlink,
        # a regular file or nothing — a FIFO swapped in cannot hang the loop's
        # executor thread here any more than at Promote.
        problems = scout_report.check(scout_report.read_text(resolved))
        if not problems:
            return ""
        return (board.REPORT_MALFORMED_REFUSAL + scout_report.brief(problems)
                + " (run python3 .claude/skills/scout/scout_check.py on it)")

    @staticmethod
    def _canonical_path(path: str) -> str:
        """`path`'s realpath spelled the way the disk spells it.

        **Blocking** (one `listdir` per component) — executor only. On a
        case-insensitive volume a realpath keeps whatever case it was typed
        in, so `…/2026-09-25-FOO/check.md` and the scan's
        `…/2026-09-25-foo/check.md` name one file under two strings, and a
        press from the list would never find the card the flag stored. Each
        component is replaced by the directory entry it names: the exact
        name when present, else the one entry that matches it without
        case; an unreadable directory keeps the rest as typed. The flag, the
        list and the press all store and compare this spelling."""
        resolved = os.path.realpath(str(path or ""))
        out = os.sep
        parts = [part for part in resolved.split(os.sep) if part]
        for index, part in enumerate(parts):
            try:
                names = os.listdir(out)
            except OSError:
                return os.path.join(out, *parts[index:])
            if part not in names:
                folded = [name for name in names
                          if name.casefold() == part.casefold()]
                if len(folded) == 1:
                    part = folded[0]
            out = os.path.join(out, part)
        return out

    @staticmethod
    def _manual_check_home(resolved: str) -> str:
        """The enrolled root a resolved check path belongs to, or `""`.

        **The one place rule** the flag, the Checks list and the outcome
        press all share: `<enrolled root>/manual-check/<folder>/check.md`,
        exactly three segments, the folder and the file name compared with
        their exact case (a realpath keeps the case it was typed in, so a
        case-folded match would store a path the list never joins to)."""
        text = str(resolved or "")
        for root in enrollment.enrolled_roots():
            base = dispatch.normalise_root(root)
            if base:
                base = BoardVerbsMixin._canonical_path(base)
            if not base or not text.startswith(base + os.sep):
                continue
            rest = text[len(base) + len(os.sep):].split(os.sep)
            if (len(rest) == 3 and rest[0] == manual_check.FOLDER
                    and rest[1] and rest[2] == manual_check.CHECK_NAME):
                return base
        return ""

    @staticmethod
    def _manual_check_path_refusal(root: str, path: str) -> tuple:
        """Validate the check file a session names. `(resolved, detail)`.

        **Blocking** (realpath, stats, one bounded read) — executor only. A
        relative path resolves inside the card's own root; the resolved file
        must then sit where `_manual_check_home` says a check lives, in the
        enrolled project the card's root belongs to (that root or an ancestor
        of it) — else `MANUAL_CHECK_PLACE_REFUSAL` — be a regular file, and
        pass `manual_check.check` — else `MANUAL_CHECK_MALFORMED_REFUSAL` and
        the problems. The same rule `_manual_check_place` applies at the
        press, so a flag the daemon accepts is a check the list shows and
        the press can reach.
        """
        base = dispatch.normalise_root(root)
        if not base:
            return "", "this card does not say which folder to work in"
        text = str(path or "").strip()
        if not text:
            return "", "a check needs a path"
        candidate = text if os.path.isabs(os.path.expanduser(text)) \
            else os.path.join(base, text)
        resolved = BoardVerbsMixin._canonical_path(
            os.path.expanduser(candidate))
        base = BoardVerbsMixin._canonical_path(base)
        home = BoardVerbsMixin._manual_check_home(resolved)
        hoisted = ""
        if not home:
            hoisted, home = BoardVerbsMixin._manual_check_side_folder(resolved)
        if not home or not (base == home or base.startswith(home + os.sep)):
            return "", board.MANUAL_CHECK_PLACE_REFUSAL
        try:
            if not stat_mod.S_ISREG(os.stat(resolved).st_mode):
                return "", "there is no file at that path"
        except OSError:
            return "", "there is no file at that path"
        problems = manual_check.check(manual_check.read_text(resolved))
        if problems:
            return "", (board.MANUAL_CHECK_MALFORMED_REFUSAL
                        + manual_check.brief(problems)
                        + " (run python3 .claude/skills/ship/manual_check.py"
                        " on it)")
        if hoisted:
            detail = BoardVerbsMixin._manual_check_hoist(resolved, hoisted)
            if detail:
                return "", detail
            resolved = hoisted
        return resolved, ""

    @staticmethod
    def _manual_check_side_folder(resolved: str) -> tuple:
        """`(main-checkout path, enrolled root)` for a check an isolated run
        wrote inside its card's side folder —
        `<root>/.worktrees/<card>/manual-check/<folder>/check.md` — else
        `("", "")`. "The project root" inside a card worktree is the
        worktree itself, so every instruction lands the file there; the
        folder is git-ignored and goes when the card's folder is removed, so
        the check is kept at the main checkout's own place instead."""
        text = str(resolved or "")
        for root in enrollment.enrolled_roots():
            base = dispatch.normalise_root(root)
            if base:
                base = BoardVerbsMixin._canonical_path(base)
            if not base or not text.startswith(base + os.sep):
                continue
            rest = text[len(base) + len(os.sep):].split(os.sep)
            if (len(rest) == 5 and rest[0] == worktrees.WORKTREES_DIR
                    and rest[1] and rest[2] == manual_check.FOLDER
                    and rest[3] and rest[4] == manual_check.CHECK_NAME):
                return (os.path.join(base, manual_check.FOLDER, rest[3],
                                     manual_check.CHECK_NAME), base)
        return "", ""

    @staticmethod
    def _manual_check_hoist(source: str, target: str) -> str:
        """Copy a side folder's check to the main checkout's place. `""` on
        success, else the refusal. Blocking — executor only. The same bytes
        already there is success; a different check already there is refused
        rather than overwritten."""
        text = manual_check.read_text(source)
        if not text:
            return "there is no file at that path"
        try:
            with open(target, encoding="utf-8") as handle:
                present = handle.read()
        except FileNotFoundError:
            present = None
        except OSError:
            return board.MANUAL_CHECK_PLACE_REFUSAL
        if present is not None:
            return "" if present == text else (
                "a different check already sits at " + target)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            spare = target + ".tmp"
            with open(spare, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(spare, target)
        except OSError:
            return board.MANUAL_CHECK_PLACE_REFUSAL
        return ""

    async def _seed_from_plan(self, card: dict, resolved: str) -> dict:
        """Fill the area and objective a just-attached plan names, where
        nobody has typed one. Both plan-attach routes call this — the
        refinement's `attach_plan_by_session` and `dark_army_add_card` given a
        `plan` — so a card gets the same fields whichever way its plan landed.
        """
        loop = asyncio.get_running_loop()
        slug = await loop.run_in_executor(None, board_workflow.read_plan_area, resolved)
        if slug:
            seeded, _ = await self._board_call("fill_area_if_empty", card["id"], slug)
            if seeded is not None:
                card = seeded
        # The objective, the same way: the plan's own header lines fill
        # the boxes nobody has typed in. This is how an agent-filed
        # card — nineteen of the twenty on this Mac's board — gets one
        # at all: `dark_army_add_card` names no objective field, and until
        # this seam the only route was the composer's Prepare.
        objective = await loop.run_in_executor(
            None, board_workflow.read_plan_objective, resolved)
        if objective:
            seeded, _ = await self._board_call(
                "fill_objective_if_empty", card["id"], objective)
            if seeded is not None:
                card = seeded
        # And the cards it waits on, from the plan's `Depends on:` header —
        # by title or id, resolved against this card's own project at the
        # moment of the attach, and written only into an empty list so a
        # person's links always win. A header naming a card that is missing
        # or ambiguous seeds nothing (a partial list would let the card
        # start early) and says why in the log; the card is attached either
        # way, since the attach is the refinement's success, not this line's.
        refs = await loop.run_in_executor(
            None, board_workflow.read_plan_depends_on, resolved)
        if refs:
            ids, refusal = await loop.run_in_executor(
                None, functools.partial(
                    self._resolve_dependency_refs, refs,
                    str(card.get("root") or ""), str(card.get("id") or "")))
            if refusal:
                logger.info("plan %s: dependencies not seeded on card %s: %s",
                            os.path.basename(str(resolved or "")),
                            str(card.get("id") or "")[:8], refusal)
            elif ids:
                seeded, detail = await self._board_call(
                    "fill_dependencies_if_empty", card["id"], ids)
                if seeded is not None:
                    card = seeded
                else:
                    logger.info("plan %s: dependencies not seeded on card "
                                "%s: %s",
                                os.path.basename(str(resolved or "")),
                                str(card.get("id") or "")[:8], detail)
        return card

    async def attach_plan_by_session(self, session_id: str, path: str, *, _author_guard=None) -> tuple:
        """Attach a plan to the card this session is refining. `(card_or_None,
        detail)`.

        The card is resolved from the session and never named by the caller —
        `close_card_by_session`'s scope restriction, and it lives here so no
        second route can skip it. Two rungs:

        1. The Prep card whose `refine_session_id` is the caller — the card a
           `refine_card` dispatch bound this session to.
        2. Failing that, **exactly one** Prep card whose `author` is the
           caller, whose `plan_path` is empty, **and which was authored
           within `ATTACH_AUTHOR_WINDOW_SECONDS`**. `author` is written
           server-side by `_handle_board_card_request` and stripped from API
           writes, so it is Dark Army's attribution, not a claim. This rung is what
           makes a hand-run `/ship` whole: Phase 5 files the card (into Prep)
           and the next tool call attaches the plan to it, landing it in
           Backlog exactly as before — with `plan_path` set. The recency
           bound is what keeps the rung that narrow: a session that filed
           one unrelated note via `dark_army_add_card` earlier in its life is
           *exactly one* authored Prep card too, and without the window this
           plan would be attached to that note — uncorrectably, since
           nothing clears `plan_path`. Two stale cards already fail closed
           on ambiguity; the window makes exactly one fail closed as well.

        Ambiguity and absence fail closed, in words. The path is validated on
        the executor (`_plan_path_refusal`) before the store sees it, and the
        store's own WHERE-clause guard answers the move-while-attaching race.

        **One exception to "more than one fails closed": a batch.** When rung
        1 finds several cards and every one carries the same `batch_id` — one
        `refine_cards` press bound them all to this session — the plan file's
        own `- **Card:**` header picks the card (`_pick_batch_member`), read
        off disk inside the shared root after `_plan_path_refusal`. The call
        still names no card; the header may choose only among the cards the
        daemon bound, and anything else fails closed listing them. The last
        batch card left reads the header too: a header naming another card
        is refused, and no header attaches to it.
        """
        if self._board is None:
            return None, "the board is not open"
        if not session_id:
            return None, "Dark Army could not tell which session asked"
        close_capture = self._refinement_root_capture(session_id)
        refined = await self._board_call("by_refine_session", session_id) or []
        candidates = [c for c in refined
                      if str(c.get("column_name") or "") == "prep"
                      and not str(c.get("plan_path") or "")]
        # Only the refinement rung may be a batch: the daemon itself bound
        # these cards to this session, so choosing among them spends nothing
        # the ladder did not already grant.
        from_refine = bool(candidates)
        if not candidates:
            prep = await self._board_call("cards", ["prep"]) or []
            cutoff = time.time() - _D.ATTACH_AUTHOR_WINDOW_SECONDS
            candidates = [c for c in prep
                          if str(c.get("author") or "") == session_id
                          and not str(c.get("plan_path") or "")
                          and float(c.get("created_at") or 0.0) >= cutoff]
        if not candidates:
            return None, ("no Prep card on Dark Army's board names this session — "
                          "file one with dark_army_add_card (bob_add_card "
                          "in a session started before the rename) first")
        loop = asyncio.get_running_loop()
        marks = {str(c.get("batch_id") or "") for c in candidates}
        if from_refine and len(marks) == 1 and "" not in marks:
            # **The batch rung.** One session refining several cards names the
            # card through the plan file's own `- **Card:**` header, never
            # through the call — the tool still takes a path and nothing else
            # — and the header may only choose among the cards this session
            # is already bound to. No header, or one naming anything else,
            # fails closed listing the members. **The last batch card left is
            # still a batch card**: its header is read too, and a header
            # naming another card is refused rather than landing that plan
            # here for good (nothing clears `plan_path`); with no header the
            # lone card takes the plan, as it would alone. A card with no
            # `batch_id` never reaches this, so a single Refine reads no
            # header.
            def batch_pick():
                shared = {dispatch.normalise_root(c.get("root") or "")
                          for c in candidates}
                if len(shared) != 1:
                    return None, None
                resolved, refusal = self._plan_path_refusal(
                    candidates[0].get("root") or "", path)
                if refusal:
                    return None, refusal
                return board_workflow.read_plan_card(resolved), ""
            named, refusal = await loop.run_in_executor(None, batch_pick)
            if named is None and refusal is None:
                return None, ("this session matches more than one Prep card — "
                              "a human has to sort that out on the board")
            if refusal:
                return None, refusal
            card = self._pick_batch_member(candidates, named)
            if card is None and len(candidates) == 1 and not named:
                card = candidates[0]
            if card is None:
                members = "\n".join(
                    f"{c.get('id') or ''} — {str(c.get('title') or '').strip()}"
                    for c in candidates)
                return None, BATCH_ATTACH_AMBIGUOUS_REFUSAL.format(
                    members=members)
        elif len(candidates) > 1:
            return None, ("this session matches more than one Prep card — "
                          "a human has to sort that out on the board")
        else:
            card = candidates[0]
        resolved, refusal = await loop.run_in_executor(
            None, self._plan_path_refusal, card.get("root") or "", path)
        if refusal:
            return None, refusal
        def close_facts():
            return (os.path.realpath(card.get("root") or ""),
                    _D.codex_rollouts._journal_identity(close_capture[1].path)
                    if close_capture else None)
        project_root, journal = await loop.run_in_executor(None, close_facts)
        if _author_guard is not None and not _author_guard():
            return None, "Dark Army could not tell which session asked"
        attached, detail = await self._board_call(
            "attach_plan", card["id"], resolved, session_id)
        if attached is not None:
            attached = await self._seed_from_plan(attached, resolved)
            self._remember_refinement_attachment(
                session_id, attached, close_capture, project_root, journal)
            await self._publish_board()
            self._log_card_event(
                attached, "card_plan_attached", session_id=session_id,
                plan_path=os.path.basename(str(resolved or "")))
            # The one completion seam a refinement has, which is why the
            # auto-start hangs off it: no second route can skip it. Scheduled
            # rather than awaited — see `_schedule_auto_start`. The return
            # value is unchanged in every branch: the plan is attached, and
            # that is what the reply reports.
            self._schedule_auto_start(attached)
        return attached, detail

    @staticmethod
    def _pick_batch_member(candidates: list, card_id: str):
        """The card among `candidates` whose id is exactly `card_id`, or
        `None`.

        The one rule every batch verb resolves a member by: the caller may
        only choose among the cards the daemon itself bound to its session,
        and an id that is not one of them — empty, foreign, or a near miss —
        chooses nothing. Shared with the batch-implement sibling, whose
        close and report verbs read their id from a file of their own.
        """
        wanted = str(card_id or "").strip()
        if not wanted:
            return None
        for card in candidates or []:
            if str((card or {}).get("id") or "") == wanted:
                return card
        return None

    async def _handle_board_attach_request(self, msg: dict) -> dict:
        """A session attaching, through Dark Army's channel, the plan it wrote for
        the card it was asked to refine.

        The third board verb on the hook socket, and it spends nothing the
        first two did not already argue for. **The hook socket authenticates
        nothing**, so as with `_handle_board_close_request` the port is a
        claim and the session is resolved by pid at the moment of use. The
        scope is the defence, stated for the record: the message carries **no
        card id** — the schema has no property for one — so a forger who wins
        the attach race for a session can only attach an existing Markdown
        file *inside that card's own project* to the card that session is
        already recorded on. That unlocks a workflow gate, not a capability:
        nothing runs, nothing is typed anywhere, and the human still has to
        read the card and drag it. Strictly less than `dark_army_close_card`
        already tolerates. Anyone loosening the schema to take a `card_id`
        has deleted the justification, not widened an API. The batch rung
        reads the plan file, never the call, and selects only within the
        cards the daemon itself bound to this session.

        Like the other two, this deliberately does not consult `is_channel` —
        that gate would silently disable the verb in exactly the dispatched
        sessions `/ship` runs in.
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.attach_plan_by_session(
            session_id, str(msg.get("path") or ""),
            _author_guard=self._board_author_guard(port))
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s attached a plan to board card %s",
                    session_id[:12], card["id"][:8])
        return {"ok": True, "detail": "plan attached — the card moved to Backlog",
                "card_id": card["id"], "column": card["column_name"],
                "title": card["title"]}

    async def attach_report_by_session(self, session_id: str, path: str, *,
                                       _author_guard=None) -> tuple:
        """Attach a report to the scout this session is working on.
        `(card_or_None, detail)`.

        `close_card_by_session`'s resolution: the card is resolved from the
        session and never named by the caller. Ambiguity and absence fail
        closed, in those two sentences. A non-scout is refused with
        `REPORT_NOT_SCOUT_REFUSAL`. The path is validated on the executor
        (`_plan_path_refusal`) before the store sees it, and a report under
        the project's `scout/` folder must also pass `scout_report.check`
        (`_report_shape_refusal`, `REPORT_MALFORMED_REFUSAL`); a report
        anywhere else attaches unchecked. What the store records is the
        resolved absolute realpath, never the caller's relative text — the
        folder is git-ignored, so the file exists only in this checkout and
        every hand-off must quote the full path. No auto-start, no
        area/objective seeding, no column move — the card stays In progress.
        The report's answer block is read once here, on the executor
        (`scout_report.read_header`), and its verdict and recommendation are
        stored beside the path for the card face; a report with no block
        stores empty values, and Promote still re-reads the file itself.
        """
        if self._board is None:
            return None, "the board is not open"
        if not session_id:
            return None, "Dark Army could not tell which session asked"
        cards = await self._board_call("by_session", session_id) or []
        open_cards = [c for c in cards if c.get("column_name") != "done"]
        if not open_cards:
            if cards:
                return None, "that card is already done — nothing to do"
            return None, "no card on Dark Army's board names this session"
        if len(open_cards) > 1:
            return None, ("this session is on more than one card — "
                          "close it on the board")
        card = open_cards[0]
        if str(card.get("kind") or "") != board.KIND_SCOUT:
            return None, board.REPORT_NOT_SCOUT_REFUSAL
        loop = asyncio.get_running_loop()
        resolved, refusal = await loop.run_in_executor(
            None, self._plan_path_refusal, card.get("root") or "", path)
        if refusal:
            return None, refusal
        refusal = await loop.run_in_executor(
            None, self._report_shape_refusal, card.get("root") or "", resolved)
        if refusal:
            return None, refusal
        header = await loop.run_in_executor(None, scout_report.read_header, resolved)
        if _author_guard is not None and not _author_guard():
            return None, "Dark Army could not tell which session asked"
        attached, detail = await self._board_call(
            "attach_report", card["id"], resolved, session_id,
            verdict=header.get("verdict", ""),
            recommendation=header.get("recommendation", ""))
        if attached is not None:
            await self._publish_board()
        return attached, detail

    async def _handle_board_attach_report_request(self, msg: dict) -> dict:
        """A session attaching, through Dark Army's channel, the report it wrote
        for the scout it is running.

        `_handle_board_attach_request`'s body with the verb swapped. The hook
        socket authenticates nothing, so the port is a claim and the session
        is resolved by pid at the moment of use. The message carries **no
        card id** — a forger who wins the attach race can only point the
        card its session is already bound to at a Markdown file inside that
        card's own project, and nothing runs.
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.attach_report_by_session(
            session_id, str(msg.get("path") or ""),
            _author_guard=self._board_author_guard(port))
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s attached a report to board card %s",
                    session_id[:12], card["id"][:8])
        return {"ok": True, "detail": "report attached — the card stays in progress",
                "card_id": card["id"], "column": card["column_name"],
                "title": card["title"]}

    async def reset_card(self, card_id: str, fields: dict = None) -> tuple:
        """Forget a card's session and any dispatch note. `(card_or_None, detail)`.

        The recovery path the board offers in words — *send it back to Ready*,
        and *Clear* on a failed dispatch — and it is a **verb of its own** rather
        than three more names in the API's field allow-list. Two reasons, and the
        second is the one that decided it. A card's `session_id` and `link_state`
        are Dark Army's own bookkeeping: they are written by the dispatch and by the
        reconcile, and nothing outside the daemon has standing to set them to an
        arbitrary value — admitting them to the allow-list would let a request
        claim a card was `live` on somebody else's session, which is a lie the
        board would then draw. Clearing them is the only edit a surface needs,
        and it is the only edit this offers. And it is *atomic*: the column move
        and the unlink are one write, so a card can never sit in Ready still
        carrying a dead session id — which is what made it permanently
        unstartable, `dispatch.guard` refusing it as already being worked on.

        `fields` rides on top through the ordinary allow-list, so *Send back*
        moves the column in the same update.

        **The plan gate deliberately does not ride here**, even though `fields`
        can carry a `column_name` and this verb takes no `allow_unplanned`.
        Judged and accepted rather than overlooked: this is a *recovery* verb —
        every surface that calls it aims at Backlog or Prep (Back, Reopen,
        Clear, the drag-out unlink), none at In progress — and the gate is a
        human-confirmation flow, not a security boundary: the same caller can
        already pass `update_card` with `skip_plan_gate`, so gating this route
        would add a second door to a gate whose one door is already open on
        request. If a surface ever grows a reset *into* In progress, that
        surface owes the confirmation, not this verb.
        """
        # No card-arrives-in-Done hook here either, and it would be inert if
        # there were: the link is cleared in this same write, so the
        # `link_state == "live"` gate could never pass. A reset is a
        # *withdrawal*, not a completion.
        if self._board is None:
            return None, "the board is not open"
        update = dict(fields or {})
        update.update({
            "session_id": "",
            "link_state": "",
            "dispatch_error": "",
            "dispatched_at": None,
            "session_ended_at": None,
            # The refinement link too, so Clear recovers a stuck refinement
            # the same way it recovers a stuck dispatch. `plan_path` is left
            # alone: a plan that was attached is a fact, not a link.
            "refine_session_id": "",
            "refine_state": "",
            # And the batch mark that rides beside it.
            "batch_id": "",
            "batch_rank": "",
        })
        async with self._dispatch_lock, self._board_write_lock:
            before = await self._board_call("get", card_id)
            refusal = self._merge_move_refusal(
                before, str(update.get("column_name") or ""))
            if refusal:
                return None, refusal
            # Judged before the write, which clears the card's own mark.
            loop = asyncio.get_running_loop()
            owns = await loop.run_in_executor(None, self._batch_owned_by, before)
            card, detail = await self._board_call("update", card_id, update)
            # The card the batch session is on, taken back — or the last
            # card that spoke for the batch, in any column: either way
            # nothing will walk the members still waiting.
            bid = str((before or {}).get("batch_id") or "")
            if card is not None and owns and (
                    self._batch_head_of(before)
                    or not await loop.run_in_executor(
                        None, self._batch_has_owner, bid)):
                # The card a batch session was on (or was starting with) is
                # being taken back: the members it never reached leave the
                # batch with a note, as when the session ends. A waiting
                # member reset on its own (Leave batch) touches nobody else.
                await self._board_call("release_batch_waiting",
                                       str(before.get("batch_id") or ""),
                                       BATCH_LEFT_NOTE)
        if card is not None:
            # The binding baseline and the absence timer are about a link that
            # no longer exists. `_dispatch_attempts` is deliberately *not*
            # cleared: it is the cooldown, and a reset must not be a way to
            # press Start twice in a second.
            self._dispatch_baseline.pop(str(card_id), None)
            self._spawn_shell_pids.pop(str(card_id), None)
            self._spawn_pty_pids.pop(str(card_id), None)
            getattr(self, "_own_terminal_dispatch", set()).discard(str(card_id))
            self._board_missing_since.pop(str(card_id), None)
            self._refine_baseline.pop(str(card_id), None)
            self._refine_missing_since.pop(str(card_id), None)
            await self._publish_board()
        return card, detail

    @staticmethod
    def _batch_head_of(card: Optional[dict]) -> bool:
        """Whether this card is the one a batch-implement session is on, or
        is starting with: it carries the mark and is `dispatching`, or bound
        `live` and not in Done. A member the session moved past (`ended`)
        or finished (Done) is not — resetting one of those must not end a
        batch that is still running. Pure."""
        card = card or {}
        if not str(card.get("batch_id") or ""):
            return False
        link = str(card.get("link_state") or "")
        if link == "dispatching":
            return True
        return (link == "live" and bool(str(card.get("session_id") or ""))
                and str(card.get("column_name") or "") != "done")

    async def delete_card(self, card_id: str) -> tuple:
        if self._board is None:
            return False, "the board is not open"
        # Capture before any await: bind on the snapshot thread can pop
        # `_spawn_shell_pids` while we wait on the store, and a gone card
        # cannot retry. The local is the handle even if that pop wins.
        # A pty receipt closes the same way: `_close_terminal_by_pid` goes
        # through `session_io`, which asks `ptyhost.owns` first.
        shell_pid = (self._spawn_shell_pids.get(str(card_id))
                     or self._spawn_pty_pids.get(str(card_id)))
        before = await self._board_call("get", card_id)
        doomed = attachments.folders_of(
            (before or {}).get("attachments")) if before else set()
        async with self._dispatch_lock, self._board_write_lock:
            loop = asyncio.get_running_loop()
            # Re-read under the lock: `before` was read outside it, and an
            # advance or a reset may have moved the card while this waited.
            # The batch judgement is made on the card as it is now.
            current = await self._board_call("get", card_id)
            owns = await loop.run_in_executor(None, self._batch_owned_by,
                                              current)
            ok, detail = await self._board_call("delete", card_id)
            bid = str((current or {}).get("batch_id") or "")
            if ok and owns and (
                    self._batch_head_of(current)
                    or not await loop.run_in_executor(
                        None, self._batch_has_owner, bid)):
                # `reset_card`'s rule: deleting the card a batch session is
                # on releases the members it never reached.
                await self._board_call("release_batch_waiting", bid,
                                       BATCH_LEFT_NOTE)
        self._dispatch_attempts.pop(str(card_id), None)
        self._dispatch_baseline.pop(str(card_id), None)
        self._spawn_shell_pids.pop(str(card_id), None)
        self._spawn_pty_pids.pop(str(card_id), None)
        getattr(self, "_own_terminal_dispatch", set()).discard(str(card_id))
        self._board_missing_since.pop(str(card_id), None)
        self._refine_baseline.pop(str(card_id), None)
        self._refine_missing_since.pop(str(card_id), None)
        self._consults.pop(str(card_id), None)
        self._consult_attempts.pop(str(card_id), None)
        if ok:
            await self._publish_board()
            if before:
                closed = await self._close_for_deleted_card(
                    before, shell_pid=shell_pid)
                # The card's own worktree, after the close and on the same
                # rules as Done: never under a live session, never with
                # unsaved work in it. A close that landed means the shell
                # is gone; one that did not leaves the release deferred and
                # judged by the live-session test alone. The branch stays.
                # Queued, never awaited here: the reply does not wait on git.
                gone = await self._deleted_card_worktree(before)
                if gone is not None:
                    if closed:
                        gone["link_state"] = "ended"
                    self._defer_worktree_release(gone, True)
                    await self._kick_worktree_releases()
            if doomed:
                remaining = await self._board_call("cards") or []
                keep = attachments.referenced_folders(remaining)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, attachments.remove_unreferenced, doomed, keep)
        return ok, detail

    async def clear_done_cards(self, expected_count: int,
                               expected_token: str) -> tuple:
        """Clear the confirmed store-wide Done set as one board operation."""
        if self._board is None:
            return False, 0, "the board is not open"
        doomed_cards = await self._board_call("cards", ["done"]) or []
        doomed_ids = [str(c.get("id") or "") for c in doomed_cards if c.get("id")]
        doomed = attachments.referenced_folders(doomed_cards)
        result = await self._board_call(
            "clear_done", expected_count, expected_token)
        if result is None:
            return False, 0, "the board is not open"
        ok, deleted_count, detail = result
        if ok:
            for cid in doomed_ids:
                self._consults.pop(cid, None)
                self._consult_attempts.pop(cid, None)
            await self._publish_board()
            # Every cleared card's own worktree, on delete's rules: nothing
            # removed under a live session or with unsaved work in it.
            for doomed_card in doomed_cards:
                gone = await self._deleted_card_worktree(doomed_card)
                if gone is not None:
                    self._defer_worktree_release(gone, True)
            await self._kick_worktree_releases()
            if doomed:
                remaining = await self._board_call("cards") or []
                keep = attachments.referenced_folders(remaining)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, attachments.remove_unreferenced, doomed, keep)
        return ok, deleted_count, detail

    async def reorder_card(self, card_id: str, column: str, before_id: str,
                           allow_unplanned: bool = False,
                           fields: Optional[dict] = None) -> tuple:
        """Place a card in a column, before `before_id` (empty = append).

        The `board_reorder` API action. A drag back to a startable column
        (Backlog or Prep) unlinks in the same write — the bug
        `BoardCardView.move` already documents, now on this path too.
        Same-column shuffling does not wipe a link. The plan gate rides here
        as well as in `update_card`, because this is the second of the two
        doors onto a column move (`_after_board_write`'s stated lesson) and a
        gate on only one of them works for half the drags.

        `fields` rides the same store `update` through `reorder`'s `extra`
        seam — the one the Backlog unlink already uses — so a drop that
        changes the lane *and* the slot is one statement and one
        `_after_board_write`, never a card left half-moved. What may arrive
        is gated by `ApiServer._BOARD_FIELDS`, not re-decided here.
        """
        if self._board is None:
            return None, "the board is not open"
        async with self._board_write_lock:
            current = await self._board_call("get", card_id)
            if current is None:
                return None, "no such card"
            dest = str(column or "") or str(current.get("column_name") or "")
            refusal = self._merge_move_refusal(current, dest)
            if refusal:
                return None, refusal
            if (not allow_unplanned and dest == "in_progress"
                    and str(current.get("column_name") or "") != "in_progress"):
                refusal = await self._plan_gate_refusal(current)
                if refusal:
                    return None, refusal
            unlink = None
            if dest in ("backlog", "prep") \
                    and str(current.get("column_name") or "") != dest:
                unlink = {
                    "session_id": "",
                    "link_state": "",
                    "dispatch_error": "",
                    "dispatched_at": None,
                    "session_ended_at": None,
                }
            extra = dict(fields or {})
            if unlink is not None:
                # The unlink wins over anything the surface sent: those keys
                # are not in `_BOARD_FIELDS` anyway, so this is belt over
                # braces rather than a live conflict.
                extra.update(unlink)
            card, detail = await self._board_call(
                "reorder", card_id, dest, str(before_id or ""), extra or None)
        if card is not None:
            if unlink is not None:
                self._dispatch_baseline.pop(str(card_id), None)
                self._spawn_shell_pids.pop(str(card_id), None)
                self._spawn_pty_pids.pop(str(card_id), None)
                self._board_missing_since.pop(str(card_id), None)
            await self._publish_board()
            # The second of the two doors onto `BoardStore.update` — this is the
            # drop into the gap between two cards, which a hook on `update_card`
            # alone would silently miss.
            await self._after_board_write(current, card)
        return card, detail

    async def dispatch_card(self, card_id: str,
                            allow_unplanned: bool = False,
                            queued_replay: bool = False,
                            own_terminal: Optional[bool] = None) -> tuple:
        """Start the assistant a card names, in that card's own project. (ok, detail)

        The one verb in this app that creates a process nobody asked for by
        typing. Every guard it has is in `dispatch.py`, argued there; this method
        is the single place they are all applied, which is what makes "can
        anything else start a session?" answerable with a grep for callers.

        Ordering is act-first, mutate-second, exactly as `wrap_up_session` does
        it: a refused spawn leaves the card untouched, so a card that says it is
        starting is one where a terminal really did open.

        **Serialised by `_dispatch_lock`.** The guard is computed from the board,
        the project roots and the in-flight list, and three awaits then pass
        before the card is marked `dispatching` — so two overlapping presses
        would each be guarded against a state neither had claimed, and each
        would open a terminal. Everything the bound depends on has to be read
        and written without another dispatch interleaving, which is what makes
        `MAX_CONCURRENT_DISPATCH`, the one-per-project rule and the cooldown
        enforcement rather than arithmetic.
        """
        # The preference first, above everything: the refusal has to be in one
        # place, or a surface that forgot to check it would route around it.
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        if self._board is None:
            return False, "the board is not open"
        async with self._dispatch_lock:
            ok, detail = await self._dispatch_card_locked(
                card_id, allow_unplanned, queued_replay,
                own_terminal=own_terminal)
        # The press answered the ask, whichever way the start went: a
        # started or queued card no longer needs asking about.
        if ok:
            self._start_ask_map().pop(str(card_id or ""), None)
        return ok, detail

    # -- Mission Control's ask to start a card ---------------------------

    def _start_ask_map(self) -> dict:
        """`card_id → {id, asked_at, by}`. Loop thread writes; the snapshot
        decoration on the executor only reads single keys."""
        asks = getattr(self, "_start_asks", None)
        if asks is None:
            asks = {}
            self._start_asks = asks
        return asks

    def _live_start_ask(self, card: dict) -> Optional[dict]:
        """The ask on this card if it is fresh and the card could still be
        started, else None. Pure over the card and the clock."""
        asks = getattr(self, "_start_asks", None) or {}
        ask = asks.get(str(card.get("id") or ""))
        if not ask:
            return None
        if time.time() - float(ask.get("asked_at") or 0) > START_ASK_TTL_SECONDS:
            return None
        if card.get("column_name") not in dispatch._STARTABLE_COLUMNS:
            return None
        if card.get("session_id") or card.get("link_state") == "dispatching":
            return None
        if card.get("refine_state") in ("dispatching", "live"):
            return None
        # Queued by a press (a full project): answered, just waiting its turn.
        if str(card.get("queue_state") or "") == "queued":
            return None
        return ask

    async def drop_start_ask(self, card_id: str) -> bool:
        """Forget an ask (the person's Dismiss). True when there was one."""
        dropped = self._start_ask_map().pop(str(card_id or ""), None)
        if dropped is not None:
            await self._publish_board()
        return dropped is not None

    async def ask_start(self, session_id: str, card_id: str) -> tuple:
        """Mission Control asks for a card to be started. `(card, detail)`.

        **This starts nothing.** It puts the card on Needs you, on the Mac and
        the phone, as "start asked"; the entry opens the card, and only the
        person's own press on START — `dispatch_card`, the ordinary verb with
        every guard it has — starts it. Dismiss on the entry is the No. So the
        rule that only a deliberate human gesture puts work in front of the
        launcher is untouched: this verb adds a way to be *asked*, not a way
        to start.

        Only the Mission Control session may ask. Any other session is
        refused, so an agent working a card cannot put asks for other work
        in front of the person.
        """
        mission_sid = str((self.mission_snapshot() or {}).get("session_id") or "")
        if not session_id or not mission_sid or session_id != mission_sid:
            return None, "only Mission Control can ask Dark Army to start a card"
        if not self.board_dispatch_enabled:
            return None, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        card_id = str(card_id or "").strip()
        if not card_id or len(card_id) > 64:
            return None, "name the card by its id"
        card = await self._board_call("get", card_id)
        if not card:
            return None, "no card has that id"
        asks = self._start_ask_map()
        now = time.time()
        for cid in [c for c, a in asks.items()
                    if now - float(a.get("asked_at") or 0) > START_ASK_TTL_SECONDS]:
            asks.pop(cid, None)
        if card.get("column_name") not in dispatch._STARTABLE_COLUMNS:
            return None, "only a card in Prep, Backlog or In progress can be started"
        if card.get("session_id") or card.get("link_state") == "dispatching":
            return None, "this card is already being worked on"
        if card.get("refine_state") in ("dispatching", "live"):
            return None, "this card is being refined — wait for the planner"
        if card_id not in asks and len(asks) >= MAX_START_ASKS:
            return None, (f"{MAX_START_ASKS} starts are already waiting on the "
                          "person — let them answer those first")
        asks[card_id] = {"id": secrets.token_hex(8), "asked_at": now,
                         "by": session_id}
        await self._publish_board()
        return card, "asked"

    async def _handle_board_start_ask_request(self, msg: dict) -> dict:
        """`dark_army_request_start`, through Dark Army's channel.

        The hook socket authenticates nothing, so the caller is resolved from
        the port (`_board_request_session_fresh`), never taken from the
        message, and must be Mission Control. The card id does cross this
        boundary — unlike the close and attach verbs — and that is safe for
        the reason `ask_start` gives: the worst a forger achieves is an entry
        on Needs you that the person dismisses. Nothing starts without their
        press on START.
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.ask_start(session_id, str(msg.get("card_id") or ""))
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("Mission Control asked to start board card %s",
                    str(card.get("id") or "")[:8])
        return {"ok": True, "detail": detail, "card_id": card.get("id"),
                "column": card.get("column_name"), "title": card.get("title"),
                "project": card.get("project")}

    #: How many Backlog cards one `start_project` press will walk. Above
    #: `board.MAX_QUEUED_PER_PROJECT` (8) plus the parallel clamp's ceiling
    #: (4), so the bound can never truncate a batch that would otherwise have
    #: fitted — by the twelfth card either the queue is full (the walk stops
    #: on the store's own refusal) or every place is taken. It is also the
    #: bound on how long `_dispatch_lock` is held: each card pays a
    #: `_known_project_roots()` and a `_claiming_session_ids()` hop, and no
    #: other Start may land meanwhile. If a batch is ever measured over a
    #: couple of seconds the fix is a smaller number here, never hoisting
    #: those reads out of the per-card judgment — that would be a second copy
    #: of it.
    START_PROJECT_MAX_CARDS = 12

    async def start_project(self, root: str) -> tuple:
        """Press Start on this project's whole Backlog, in board order. `(ok, detail)`

        One gesture, N presses. It is deliberately **not** a new capability:
        every card goes through `_dispatch_card_locked`, which applies the
        plan gate, the enrolment refusal,
        `dispatch.guard` and the slot gate exactly as a hand press does, so
        nothing here can start work a person could not have started one card
        at a time. There is no `allow_unplanned` and there never will be — an
        unplanned card is skipped and reported, because a confirmation is a
        thing a person gives about *one* card.

        It spawns at most one process. `dispatch.guard`'s
        `PROJECT_BUSY_REFUSAL` is transient, so the first card dispatches and
        every card behind it joins the queue; the drain
        (`_decide_queue_dispatches`) starts the rest at the project's own
        parallel limit. Nothing here counts what is running — `board_queue`
        still owns that, read inside `_slot_refusal`.

        `_dispatch_lock` is taken **once**, so the whole batch is one
        serialised gesture and no other press interleaves halfway through.
        No `_board_write_lock`: the batch writes no card itself.

        **No event-log kind.** The batch is N presses and today N hand presses
        write exactly this: one `card_dispatched` from
        `_dispatch_card_locked` for the card that actually started, and
        nothing for a card that joined the queue — joining a queue is not a
        moment the diary records. Six "queued" lines would be six entries one
        press produced for something the board already shows, and a synthetic
        "batch" kind would be a sentence no other path can ever write.
        """
        # The preference first, above the lock and above everything else, and
        # the same sentence `dispatch_card` refuses with: a second wording
        # here would be a second place to forget.
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        if self._board is None:
            return False, "the board is not open"
        wanted = dispatch.normalise_root(root)
        if not wanted:
            return False, "no folder named"
        loop = asyncio.get_running_loop()
        async with self._dispatch_lock:
            # Already in the board's own order (`column_name, position,
            # created_at`), so the batch needs no sort of its own — the line
            # it forms is the line the person is looking at.
            cards = await self._board_call("cards", ["backlog"]) or []
            # By **root**, never by the project label: two folders can wear
            # one name, and a batch that picked up the other one's cards
            # would start work in a project nobody pressed anything in.
            mine = [c for c in cards
                    if dispatch.normalise_root(str(c.get("root") or "")) == wanted]
            already = [c for c in mine
                       if str(c.get("queue_state") or "") == "queued"]
            todo = [c for c in mine
                    if str(c.get("queue_state") or "") != "queued"]
            walk = todo[:self.START_PROJECT_MAX_CARDS]
            remaining = len(todo) - len(walk)
            remaining_reason = "cap" if remaining else ""
            started = 0
            queued = 0
            skips: list = []
            for index, card in enumerate(walk):
                cid = str(card.get("id") or "")
                # No `allow_unplanned`, and no `queued_replay`: this is a
                # person's press repeated, and a truthy `queued_replay`
                # would make `_enqueue_card` return silently *without
                # writing `queue_state`* — one card would start and nothing
                # would be lined up at all. `_flush_queue_dispatches` is the
                # only caller that may pass it.
                ok, detail = await self._dispatch_card_locked(cid)
                if ok:
                    started += 1
                    continue
                # `_enqueue_card` and a hard refusal both answer
                # `(False, detail)`, so the card itself is the honest signal.
                # Matching the prose would be a contract nobody declared.
                fresh = await self._board_call("get", cid) or {}
                if str(fresh.get("queue_state") or "") == "queued":
                    queued += 1
                    continue
                skips.append((str(card.get("title") or ""), detail))
                # The store's own ceiling, asked as a number rather than read
                # out of a sentence. Once a project's queue is full every card
                # behind this one would earn the same refusal, so the walk
                # stops and the rest are reported as left where they were.
                project = str(card.get("project") or "")
                full = await loop.run_in_executor(
                    None, functools.partial(self._queue_is_full, project))
                if full:
                    remaining += len(walk) - index - 1
                    remaining_reason = "queue_full"
                    break
            logger.info("start project %s: %d started, %d queued, %d skipped, "
                        "%d left", wanted, started, queued, len(skips),
                        remaining)
            return (started + queued > 0,
                    _D._start_project_report(
                        started, queued, len(already), skips,
                        remaining=remaining,
                        remaining_reason=remaining_reason,
                        max_cards=self.START_PROJECT_MAX_CARDS))

    def _queue_is_full(self, project: str) -> bool:
        """Whether this project's queue is at `MAX_QUEUED_PER_PROJECT`.
        Blocking (it reads the store); handed to the executor by its one
        caller. The number, never the store's refusal sentence."""
        if self._board is None:
            return False
        try:
            return (self._board.queued_count(project)
                    >= board.MAX_QUEUED_PER_PROJECT)
        except Exception:
            logger.warning("could not read the queue depth", exc_info=True)
            return False

    def _uses_own_terminal(self, card_id: str,
                           own_terminal: Optional[bool]) -> bool:
        """Whether this press (or its queued replay) opens Dark Army's own pty.

        `True` is a person's START HERE: remember it so a later drain
        honours the same place. `False` is an ordinary Start: forget a
        leftover HERE so the later press wins. `None` is the drain /
        `start_project` / auto-start — they never change the set, they
        only read it, falling back to the machine-wide preference.
        """
        cid = str(card_id)
        remembered = getattr(self, "_own_terminal_dispatch", None)
        if remembered is None:
            remembered = set()
            self._own_terminal_dispatch = remembered
        if own_terminal is True:
            remembered.add(cid)
            return True
        if own_terminal is False:
            remembered.discard(cid)
            return bool(getattr(self, "board_own_terminal_enabled", False))
        return cid in remembered or bool(
            getattr(self, "board_own_terminal_enabled", False))

    async def _dispatch_card_locked(self, card_id: str,
                                    allow_unplanned: bool = False,
                                    queued_replay: bool = False,
                                    own_terminal: Optional[bool] = None,
                                    batch: Optional[list] = None) -> tuple:
        """The body of `dispatch_card`, under `_dispatch_lock`. No other caller
        but the batch press (`_start_cards_locked`), which passes `batch`.

        `queued_replay` says the caller is the queue's drain rather than a
        person, and it changes **two things**. First, what happens to a card
        that cannot start *yet*: a person's press on colliding work enqueues
        the card; the drain's replay of an already-queued card leaves it
        queued and says nothing, to be retried on the next reconcile. Second,
        the plan gate's two confirmations — no plan, or a plan changed since
        approval — are not asked again: a queued card is a confirmed card
        (`_plan_gate_refusal(replay=True)`), and only a plan file that has
        gone still refuses. Every other refusal is identical in both
        directions, which is the point — the drain is not a privileged path,
        it re-runs the same guards a fresh press would hit, and a card it
        cannot start is *dequeued* with the refusal written on it rather than
        retried at reconcile cadence for ever.

        `batch` (the batch-implement press, and only it) is the ordered list
        of every card one session will work, this card first. It changes
        three things and no guard: the prompt is
        `dispatch.implement_batch_prompt(batch)`; a card that could only
        *wait* — a transient guard refusal, an unmet dependency, a full
        project — is refused in words and **never enqueued**, because a
        queue of a batch is not a thing the drain knows how to start; and
        the store write carries a fresh `batch_id` with `batch_rank` 1,
        handed back on `self._last_batch_token`. Without `batch` the update
        dict and the argv are byte-identical to what they were.
        """
        self._last_batch_token = ""
        card = await self._board_call("get", card_id)
        # A batch member still waiting its turn is refused before anything
        # else, hard, never queued: it will be bound by its own session's
        # `dark_army_next_card`, and a second session on it would be two
        # sessions racing for one card. Leave batch (`board_reset`) frees it.
        if card is not None and batch is None and _batch_marked_unbound(card):
            # Still waiting in Backlog, or dragged out of it with its mark:
            # either way the batch's own session is the one to bind it.
            refusal = (BATCH_WAITING_REFUSAL if _batch_waiting(card)
                       else BATCH_MEMBER_REFUSAL)
            return await self._queue_hard_refusal(card, refusal, queued_replay)
        # The plan gate, before the guard: a dispatch is an arrival in
        # In progress by construction, and the refusal names the way through
        # (the confirmed press carries `allow_unplanned`). A missing card
        # falls through to the guard's own "no such card".
        #
        # `allow_unplanned` is not carried across an enqueue, and it does not
        # need to be: a card is only ever `queued` because it passed, or was
        # confirmed past, this very gate at the press, so `queued` itself is
        # the record of the answer. Re-asking on the replay would be asking a
        # question the person already answered, and the drain answering "no"
        # for them — dequeuing with an orange note — was the bug. The one
        # rung that fires on a replay is the vanished plan file, because
        # `dispatch.start_prompt` would build `/ship implement <missing
        # path>`.
        if card is not None and not allow_unplanned:
            refusal = await self._plan_gate_refusal(card, replay=queued_replay)
            if refusal:
                return await self._queue_hard_refusal(
                    card, refusal, queued_replay)
        if card is not None:
            refusal = await self._enrollment_refusal(card)
            if refusal:
                return await self._queue_hard_refusal(card, refusal,
                                                      queued_replay)
        loop = asyncio.get_running_loop()
        roots = await loop.run_in_executor(None, self._known_project_roots)
        use_own = self._uses_own_terminal(card_id, own_terminal)
        if use_own:
            # With Dark Army's own terminal on, Start needs no editor window: the
            # card's root may be any **enrolled** project folder, not only
            # one with a VS Code window open or a session already in it.
            # `guard()` still demands an exact member that is a real
            # directory, and `_enrollment_refusal` already ran above — this
            # widens the *known* set to the ledger, and is not a bypass of
            # the enrolment gate for "Dark Army started it".
            enrolled = await loop.run_in_executor(None, enrollment.enrolled_roots)
            roots = set(roots) | {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
        all_cards = await self._board_call("cards") or []
        # Both kinds of launch count against the bound: a refinement is a new
        # claude session in the card's project, and binding by elimination
        # only works while "the first new session in this project" is a
        # single session — whichever verb produced it.
        in_flight = self._launch_inflight(all_cards)

        ok, detail = dispatch.guard(
            card, roots=roots, in_flight=in_flight, now=time.time(),
            last_attempt=self._dispatch_attempts.get(str(card_id)))
        if not ok:
            if batch is not None:
                # A batch is never queued: the words, and nothing written.
                return False, detail
            if card is not None and dispatch.is_transient(detail):
                # It will pass on its own. A person's press joins the queue; a
                # replay simply waits where it already is. A card that also
                # waits on an unfinished card says so rather than blaming the
                # in-flight bound — the dependency is the longer wait.
                unmet = None
                if not queued_replay:
                    unmet = await loop.run_in_executor(
                        None, functools.partial(
                            self._unmet_dependencies, card,
                            {c["id"]: c for c in all_cards if c.get("id")},
                            self._dependency_running_ids(
                                self._agents_snapshot_cache)))
                return await self._enqueue_card(card, queued_replay,
                                                unmet=unmet or None)
            return await self._queue_hard_refusal(card, detail, queued_replay)

        # The queue gate, *after* `guard`. Here rather than inside `guard`
        # because `guard` is pure and this reads the whole board; here rather
        # than at the two call sites because Start and the drop into
        # In progress both arrive as `board_dispatch` and a gate in one of
        # them is a gate for half the gestures.
        #
        # After `guard` on purpose, and the order is load-bearing: the queue
        # may only ever hold a card that could actually start once a place
        # frees. With the gate first, a full project turned every one of
        # `guard`'s *hard* refusals — no tool named, no known root, a prompt
        # the CLI would read as a flag — into a queued card that would sit
        # there for ever, since the drain re-runs the same guards and a card
        # it hard-refuses is only dequeued once it is replayed. `guard`'s own
        # transient refusals (the in-flight bounds, the cooldown) still
        # enqueue, through the `is_transient` branch above.
        #
        # `active` is one reading for the gate and the enqueue's sentence —
        # two readings inside one press could disagree with each other, and
        # the walk is not free (item: compute claiming once per frame).
        if card is not None:
            active = await loop.run_in_executor(
                None, self._claiming_session_ids)
            # The dependency gate, after `guard` for the queue gate's own
            # reason (only a card that could start once it is free may be
            # queued) and before the slot rule, because a card waiting on
            # another card is not waiting for a place. It **queues, never
            # refuses**: a person's press joins the line with a sentence
            # naming what it waits for, and the drain's replay holds
            # silently — `_enqueue_card` returns before any store call —
            # until every dependency is met. Nothing here starts a card
            # nobody pressed Start on: a dependency becoming met only lets
            # the drain replay a card that is already `queued`, and the
            # replay re-runs every gate above at that instant.
            running_ids = self._dependency_running_ids(
                self._agents_snapshot_cache)
            by_id = {c["id"]: c for c in all_cards if c.get("id")}
            unmet = await loop.run_in_executor(
                None, functools.partial(
                    self._unmet_dependencies, card, by_id,
                    running_ids, active))
            if unmet:
                if batch is not None:
                    return False, BATCH_DEPENDENCY_REFUSAL
                return await self._enqueue_card(card, queued_replay,
                                                active=active, unmet=unmet)
            held = await loop.run_in_executor(
                None, functools.partial(
                    self._slot_refusal, card, all_cards, active=active,
                    running_ids=running_ids))
            if held:
                if batch is not None:
                    return False, BATCH_NO_PLACE_REFUSAL.format(
                        project=str(card.get("project") or "this project"))
                return await self._enqueue_card(card, queued_replay,
                                                active=active)

        executable = await loop.run_in_executor(
            None, dispatch.resolve_executable, card["tool"])
        if not executable:
            return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool=card['tool'])

        # Card isolation (`docs/card-worktrees.md`): where the terminal
        # opens. Every gate above has passed, on the card's own `root` —
        # property 4 is untouched, the worktree is derived from the root and
        # never a root. A git project with isolation on works in the head
        # card's worktree: reused where it is recorded and still a worktree
        # git knows, otherwise **prepared first** in a task — the fetch, the
        # `worktree add` and the setup script can take minutes and the
        # panel's POST times out in five seconds — which re-enters this very
        # verb, every gate again, once the folder is ready. Nothing is
        # written to the card here: a preparing card is not `dispatching`.
        cwd = str(card.get("root") or "")
        head = batch[0] if batch else card
        wants = await loop.run_in_executor(None, self._wants_worktree, cwd)
        if wants:
            if str(head.get("id") or "") in (
                    getattr(self, "_merging", None) or {}):
                return False, merges.START_WHILE_MERGING_REFUSAL
            reuse = await self._reusable_worktree(head)
            if reuse:
                cwd = reuse
                # A second Start refreshes the carried pack files; the
                # result is only logged (never a refusal).
                note = await loop.run_in_executor(
                    None, self._sync_pack_copies,
                    dispatch.normalise_root(str(card.get("root") or "")),
                    reuse, self._pack_card_id(reuse))
                if note:
                    logger.info("card %s: %s", str(card_id)[:8], note)
            elif str(head.get("id") or "") in (
                    getattr(self, "_worktree_preparing", None) or {}):
                return False, dispatch.WORKTREE_PREPARING_REFUSAL
            else:
                self._start_worktree_prepare(head, cwd, {
                    "card_id": str(card_id),
                    "allow_unplanned": bool(allow_unplanned),
                    "queued_replay": bool(queued_replay),
                    "own_terminal": own_terminal,
                    "batch_ids": ([str(c.get("id") or "") for c in batch]
                                  if batch is not None else None),
                })
                await self._publish_board()
                return True, worktrees.PREPARING_NOTE

        # The attachment block is Dark Army's addition, appended after the plan
        # gate and `guard` have judged the person's own text — a leading-`-`
        # check must not see it first, and appending at the end cannot create
        # a flag-shaped prompt. Never baked into the stored `prompt`: removing
        # an attachment later never means editing somebody's instructions.
        # The strip in front of it is for cards prepared *before* that was
        # true, whose stored prompt already carries the paths: it cleans the
        # spawned text only, and never what is saved.
        if batch is not None:
            prompt = await self._implement_batch_prompt(batch)
            refusal = dispatch.prompt_refusal(card["tool"], prompt)
            if refusal:
                return False, refusal
        else:
            prompt = dispatch.start_prompt(card)
            rels = attachments.split_field(card.get("attachments"))
            if rels:
                abs_paths = await loop.run_in_executor(
                    None, attachments.resolve_paths, rels)
                prompt = (attachments.strip_path_lines(prompt, abs_paths)
                          + attachments.prompt_block_from_abs(abs_paths))
            # The person's objective rides last, after the attachment block
            # and after `guard` judged the one-line prompt, so a criterion
            # beginning with `-` cannot change what the guard saw. Empty when
            # the card has none: an unobjectived card's argv is
            # byte-identical to before.
            prompt += dispatch.objective_block(card)
            slug = str(card.get("area") or "")
            brief = areas.brief_path(slug)
            brief_present = bool(brief) and await loop.run_in_executor(
                None, os.path.isfile,
                os.path.join(str(card.get("root") or ""), brief))
            prompt += dispatch.area_block(card, brief_present)

        # The card's own model wins; a card left at Default takes the
        # main-session model chosen for this assistant in this project
        # (`_agent_model_for`, the one resolution seam), which is `""` —
        # no flag, today's argv byte for byte — until somebody picks one.
        argv = dispatch.argv_for(
            card["tool"], executable, prompt,
            model=(str(card.get("model") or "")
                   or self._agent_model_for(card.get("root"), card["tool"],
                                            "main")))
        name = (card.get("title") or "agent")[:40]
        if batch is not None:
            name = (f"batch: {len(batch)} cards")[:40]
        # Everything the fleet already had. The session this launch produces is
        # the first one that is *not* in here — see `_bind_dispatched_card`.
        # Taken *before* the spawn await: the extension's reply can lose the
        # race to the new session's own SessionStart, and a baseline captured
        # after the await would then contain the very session it exists to
        # single out, so the card could never bind.
        baseline = self._live_session_ids()
        # Where the terminal opens is the `board_own_terminal` preference,
        # or a press that sent `own_terminal` (START HERE on a card with
        # no connected terminal). Read once above as `use_own` so the
        # enrolled-root widening and the spawner cannot disagree. Every
        # refusal above ran identically on both paths; refinements make the
        # same choice in `_refine_card_locked`, on the preference alone.
        spawner = dispatch.spawn_local if use_own else dispatch.spawn
        # What this session was started for, stamped into its environment at
        # the moment the terminal opens rather than guessed from a transcript
        # afterwards. The stage is the card's own next *declared* stage where
        # it declares one and `""` otherwise — never invented; a stamp that
        # names a stage nobody wrote down would be a claim, not a record.
        # Attribution, not authorisation: `origin.py`.
        declared = board.parse_stages(card.get("workflow"))
        already = set(board.parse_stages(card.get("agent_trail")))
        next_stage = next((s for s in declared if s not in already), "")
        # `cwd` only where it differs from the root, so a start in the main
        # checkout is the call the spawners always received.
        where = {"cwd": cwd} if cwd and cwd != card["root"] else {}
        spawned, spawn_detail, shell_pid = await spawner(
            card["root"], argv, name,
            stamp=origin.stamp("card-start", card["id"], next_stage),
            **where)
        if not spawned:
            if spawn_detail == dispatch.WORKTREE_WINDOW_REFUSAL:
                # Not transient: the window stays too old until somebody
                # reloads it, so a queued replay is dequeued with the words
                # rather than retried on every pass.
                return await self._queue_hard_refusal(card, spawn_detail,
                                                      queued_replay)
            return False, spawn_detail
        # The press has been spent. A queued card never reaches here, so
        # the drain can still read the set; a later ordinary Start on a
        # reset of this card must not inherit HERE.
        getattr(self, "_own_terminal_dispatch", set()).discard(card["id"])

        now = time.time()
        self._dispatch_attempts[card["id"]] = now
        self._dispatch_baseline[card["id"]] = baseline
        self._forget_worktree_state(str(card["id"]))
        # The terminal receipt, when the window reported one. A stale entry
        # from an earlier press must not outlive it, so absence pops. A pty
        # spawn's third element is the child's own pid, and it goes in the
        # *other* map: an identity, not a descent proof.
        if use_own:
            self._spawn_shell_pids.pop(card["id"], None)
            if shell_pid:
                self._spawn_pty_pids[card["id"]] = shell_pid
            else:
                self._spawn_pty_pids.pop(card["id"], None)
        else:
            self._spawn_pty_pids.pop(card["id"], None)
            if shell_pid:
                self._spawn_shell_pids[card["id"]] = shell_pid
            else:
                self._spawn_shell_pids.pop(card["id"], None)
        # `bump=False`: Dark Army moving a card it is starting is not somebody
        # editing it, and a change number that moved here would refuse the
        # save of whoever was typing into the card at the time.
        fields = {
            "link_state": "dispatching",
            # The card moves the moment the terminal opens, not when the session
            # is finally bound. Binding takes up to `DISPATCH_BIND_WINDOW`, and a
            # card sitting in Backlog for two minutes with an assistant already
            # working in its project is the board telling a lie about the one
            # thing it is for. `bind_session` writes the column again — the same
            # value — because it is also the path a card that was *never*
            # dispatched here takes.
            "column_name": "in_progress",
            "dispatched_at": now,
            "dispatch_error": "",
            # Named explicitly rather than left to `update`'s
            # clear-on-`column_name` rule: this is the one write where a card
            # both leaves the queue and starts, and a window where a card is
            # both `queued` and `dispatching` would let the drain pick it a
            # second time. Belt and braces, and the braces are one line.
            "queue_state": "",
            "queued_at": None,
        }
        if batch is not None:
            # The batch's mark, on the head alone: the waiting members are
            # marked by the caller once this write has landed. Only here, so
            # a single Start's update is byte-identical to what it was.
            self._last_batch_token = secrets.token_hex(8)
            fields["batch_id"] = self._last_batch_token
            fields["batch_rank"] = "1"
        await self._board_call("update", card["id"], fields, bump=False)
        await self._publish_board()
        # Where this project stood the moment work started, so the record
        # written at the end has something to measure against. Scheduled and
        # **never awaited**: it is one `git rev-parse` and a slow or missing
        # git must not hold the terminal that has already opened. A run whose
        # baseline never lands is still recorded at the end — it simply says
        # it had no starting point.
        # Taken in the folder the terminal opened in: a card working in its
        # own worktree is measured against its own branch, so `card_runs`'
        # root and baseline are the worktree's.
        self._schedule_work_baseline(card["id"],
                                     cwd or str(card.get("root") or ""), now)
        logger.info("dispatched card %s (%s) into %s",
                    card["id"][:8], card["tool"], cwd or card["root"])
        self._log_card_event(card, "card_dispatched",
                             tool=str(card.get("tool") or ""),
                             root=str(card.get("root") or ""))
        return True, spawn_detail

    # --- card dependencies (docs/card-dependencies.md) ---------------------

    def _dependency_entries(self, card: dict, by_id: dict,
                            running_ids: Optional[set],
                            active: Optional[set], *,
                            fetch: bool = True) -> list:
        """Every dependency of `card` that still names a card, with its met
        bit: `[{id, title, column_name, met}]` in the stored order.
        Blocking where `fetch` reads the store; executor only.

        **The one resolver.** The gate, the drain and the decoration all
        come through here with the same `running_ids` / `active` pair, and
        "met" is `board_queue.dependency_met` over `_card_session_working` —
        the manual-check badge's own predicate — so the tile, the gate and
        the drain cannot disagree about one card. An id `by_id` lacks is
        looked up with one `cards_by_id` read when `fetch` is on (a
        dependency finished a week ago is outside the frame); an id that
        names no card at all is **met and left out** — the rule, not an
        accident of what the frame happened to hold.
        """
        ids = board.parse_ids((card or {}).get("blocked_by"))
        if not ids:
            return []
        known = by_id or {}
        missing = [i for i in ids if i not in known]
        extra: dict = {}
        if missing and fetch and self._board is not None:
            extra = self._board.cards_by_id(missing)
        sessions = active if active is not None else set()
        out = []
        for dep_id in ids:
            dep = known.get(dep_id) or extra.get(dep_id)
            if dep is None:
                continue
            if running_ids is None:
                # No agents snapshot yet (the seconds after a restart): who
                # is working is unknown, so a bound session counts as
                # working — a press in that window waits rather than
                # starting beside a dependency still being worked on.
                working = str(dep.get("link_state") or "") in (
                    "dispatching", "live")
            else:
                working = self._card_session_working(
                    dep, running_ids, sessions)
            out.append({
                "id": str(dep.get("id") or dep_id),
                "title": str(dep.get("title") or ""),
                "column_name": str(dep.get("column_name") or ""),
                "met": board_queue.dependency_met(dep, working),
            })
        return out

    @classmethod
    def _dependency_running_ids(cls, snapshot) -> Optional[set]:
        """`_board_running_ids(snapshot)`, or **None** where no agents
        snapshot has arrived yet — `_dependency_entries`' "unknown", read
        conservatively. The badge keeps its own self-correcting reading."""
        snapshot = snapshot or {}
        if not any(k in snapshot for k in
                   ("running", "waiting", "sleeping", "finished")):
            return None
        return cls._board_running_ids(snapshot)

    def _unmet_dependencies(self, card: dict, by_id: dict,
                            running_ids: Optional[set] = None,
                            active: Optional[set] = None, *,
                            fetch: bool = True) -> list:
        """The dependencies of `card` that hold it, `_dependency_entries`
        filtered to `met` false. Executor only. `running_ids` is the caller's
        `_dependency_running_ids` reading — None meaning no agents snapshot
        yet, read conservatively — and `active` defaults to one
        `_claiming_session_ids()` walk. `fetch=False` is for a caller whose
        `by_id` is already the whole board, where an id it lacks names no
        card and a store read would find nothing."""
        if active is None:
            active = self._claiming_session_ids()
        return [e for e in self._dependency_entries(
            card, by_id, running_ids, active, fetch=fetch) if not e["met"]]

    def _dependency_held_ids(self, cards: list, running_ids: Optional[set],
                             active: Optional[set]) -> set:
        """The ids of the **queued** cards in `cards` that an unmet dependency
        holds. What `board_queue.eligible` drops from the drain's head and
        from `_slot_refusal`'s "queued ahead" rung, so a card that cannot
        start never stands in another's way. Executor only. Both callers
        hand the whole board (`BoardStore.cards()`), so no id is outside it
        and the store is not asked again — this runs on every reconcile."""
        by_id = {c["id"]: c for c in cards or () if c.get("id")}
        held = set()
        for card in cards or ():
            if str(card.get("queue_state") or "") != "queued":
                continue
            if not board.parse_ids(card.get("blocked_by")):
                continue
            if self._unmet_dependencies(card, by_id, running_ids, active,
                                        fetch=False):
                held.add(str(card.get("id") or ""))
        return held

    def _dependency_frame(self, cards: list) -> tuple:
        """`(by_id, dependents)` for one snapshot. Blocking; executor only.

        `by_id` is the frame's cards plus every dependency they name that
        the frame does not hold, read in **one** `cards_by_id` — never a read
        per card. `dependents` is the reverse map, `{dependency id: [{id,
        title}]}`, built off the frame's own cards and leaving Done ones out:
        "unblocks" a finished card would tell nobody anything.
        """
        by_id = {c["id"]: c for c in cards or () if c.get("id")}
        wanted = []
        dependents: dict = {}
        for card in cards or ():
            ids = board.parse_ids(card.get("blocked_by"))
            for dep_id in ids:
                if dep_id not in by_id and dep_id not in wanted:
                    wanted.append(dep_id)
            if not ids or str(card.get("column_name") or "") == "done":
                continue
            for dep_id in ids:
                dependents.setdefault(dep_id, []).append({
                    "id": str(card.get("id") or ""),
                    "title": str(card.get("title") or "")})
        if wanted and self._board is not None:
            by_id = dict(by_id)
            by_id.update(self._board.cards_by_id(wanted))
        return by_id, dependents

    def _resolve_dependency_refs(self, refs, root: str,
                                 exclude_id: str = "") -> tuple:
        """Card ids for a list of references, or a refusal in words.
        `(ids, refusal)`; blocking (it reads the board), executor only.

        Each reference is a card id **or an exact title** — trimmed,
        case-insensitive — among the cards of the same project folder
        (Done included, the card itself excluded). One reference that names
        no card, or more than one, refuses the **whole** list: a partial list
        is a card that starts before something it was meant to wait for.
        Bounded at `board.MAX_BLOCKERS` references, each clamped at
        `MAX_DEPENDENCY_REF_CHARS`.
        """
        wanted: list = []
        for ref in refs or ():
            text = str(ref or "").strip()[:MAX_DEPENDENCY_REF_CHARS]
            if text and text not in wanted:
                wanted.append(text)
            if len(wanted) >= board.MAX_BLOCKERS:
                break
        if not wanted:
            return [], ""
        mine = dispatch.normalise_root(str(root or ""))
        if not mine:
            return [], ("this card names no project folder, so it cannot "
                        "wait on another card")
        if self._board is None:
            return [], "the board is not open"
        roots: dict = {}

        def same_project(card: dict) -> bool:
            raw = str(card.get("root") or "")
            if raw not in roots:
                roots[raw] = dispatch.normalise_root(raw)
            return roots[raw] == mine

        pool = [c for c in self._board.cards()
                if str(c.get("id") or "") != str(exclude_id or "")
                and same_project(c)]
        ids: list = []
        for ref in wanted:
            by_id = [c for c in pool if str(c.get("id") or "") == ref]
            if by_id:
                ids.append(str(by_id[0]["id"]))
                continue
            named = [c for c in pool
                     if str(c.get("title") or "").strip().lower() == ref.lower()]
            if not named:
                return [], f'no card in this project is called "{ref}"'
            if len(named) > 1:
                return [], (f'"{ref}" names more than one card in this '
                            "project — use its id")
            ids.append(str(named[0]["id"]))
        return board.parse_ids(ids), ""

    # --- the per-project work queue -----------------------------------

    def _slot_refusal(self, card: dict, all_cards: list,
                      active: Optional[set] = None,
                      running_ids: Optional[set] = None) -> bool:
        """Whether this card must wait for a place in its project. Blocking-ish
        (it walks the board); handed to the executor by its one caller, which
        also hands it the frame's one `_claiming_session_ids()` reading as
        `active` so the gate and the sentence cannot disagree.

        **The whole steady-state waiting rule since 24 Aug 2026.** A project
        may have `_parallel_limit_for(root)` agents working at once — its own
        dial where it has one, the machine default where it has not; this card
        waits when it has none free. The file-overlap comparison that used to
        decide this was removed 5 Sep 2026. The trade-off above a
        limit of 1 (two agents in one tree, the same files included) is the
        accepted one and is deliberately not mitigated here.

        Returns a bool rather than a card, unlike the gate it replaces: under
        the slot rule *no single card* is the holder — the project is full —
        and naming one of the runners would be blaming an arbitrary card for a
        wait it does not own. The sentence a person reads is composed from the
        count instead (`_queue_reason`).

        Two kinds of wait, and the second is what makes the queue an order
        rather than a scramble:

        1. **No free place.** `board_queue.claims` with `_claiming_session_ids`
           — the same predicate the snapshot publishes as `work_active`, one
           reading, never a second count filtering `link_state` inline: that
           second count is exactly the 23 Aug wedge where a finished-but-open
           terminal held a pipeline for an hour.
        2. **No overtaking.** A card of the same project is queued ahead of
           this one. Which cards count depends on whether this card is *in*
           the line yet, and the asymmetry is load-bearing: a queued card —
           the drain replaying the head — is held only by cards queued
           *before* it, because holding it against the ones behind it is the
           deadlock of 23 Aug, where each of a pair was in the other's way and
           neither ever started. A card that is not queued is a fresh press
           joining the back, so every queued card is ahead of it; that case
           cannot be left to the sort key, since an unqueued card's `queued_at`
           of None reads as 0.0 and would jump the whole queue.

        A queued card held by an unmet dependency is **not** ahead of anyone
        (`board_queue.eligible`): it cannot start, so counting it would let A,
        waiting on B and queued first, hold B for ever — the same deadlock one
        rung on. `running_ids` is the caller's reading for that check, the
        cached agents snapshot's where it has none.
        """
        project = str(card.get("project") or "")
        cid = str(card.get("id") or "")
        if active is None:
            active = self._claiming_session_ids()
        running = [c for c in board_queue.claims(all_cards, project, active)
                   if str(c.get("id") or "") != cid]
        if len(running) >= self._parallel_limit_for(card.get("root")):
            return True
        queued = [c for c in all_cards
                  if str(c.get("project") or "") == project
                  and str(c.get("queue_state") or "") == "queued"
                  and str(c.get("id") or "") != cid]
        if queued:
            if running_ids is None:
                running_ids = self._dependency_running_ids(
                    self._agents_snapshot_cache)
            queued = board_queue.eligible(queued, self._dependency_held_ids(
                all_cards, running_ids, active))
        if str(card.get("queue_state") or "") == "queued":
            mine_key = board_queue.queue_key(card)
            queued = [c for c in queued
                      if board_queue.queue_key(c) < mine_key]
        return bool(queued)

    async def _enqueue_card(self, card: dict, queued_replay: bool,
                            active: Optional[set] = None,
                            unmet: Optional[list] = None) -> tuple:
        """Hold this card in its project's queue. Always `(False, detail)`.

        `active` is the caller's `_claiming_session_ids()` reading, where it
        has one, so the sentence below is composed from the same count the
        gate just judged. `unmet` is the dependency gate's list, where that
        is why the card waits: the sentence then names those cards
        (`_dependency_reason`) instead of counting agents — the badge's own
        words a frame later, by the decoration's same rule.

        `ok=False` keeps `dispatch_card`'s invariant that True means a terminal
        really opened. The panel shows the sentence once on the refusal line;
        the durable truth is the card's own queued badge from the next
        snapshot, which is why nothing here has to be remembered by a surface.

        A card the drain is replaying is already queued — nothing is written,
        because a re-stamp of `queued_at` would move it to the back of a queue
        it is at the front of, and it returns quietly: the person made this
        gesture minutes ago and does not need it reported again. A *person*
        repeating the gesture on that same card is the other half of what used
        to be one collapsed branch, and it does get words
        (`_already_queued_reason`) — still without writing anything.

        The refusal it answers with is the **same sentence** the card's own
        badge will wear a snapshot later (`_queue_reason`), composed from the
        same count: the reply and the badge saying two different things about
        one wait is how a queue stops being readable.
        """
        if queued_replay:
            # The drain replaying the head of the line. Silent by design: the
            # person made this gesture minutes ago and does not need it
            # reported again, and a board read per drain pass buys nothing.
            return False, ""
        if str(card.get("queue_state") or "") == "queued":
            # A person's repeat press or drop. Read-only by construction —
            # re-stamping `queued_at`/`queue_rank` here would move the card to
            # the back of a queue it is near the front of — so the whole
            # branch is one read and a sentence.
            all_cards = await self._board_call("cards") or []
            project = str(card.get("project") or "")
            cid = str(card.get("id") or "")
            mine_key = board_queue.queue_key(card)
            ahead = [c for c in all_cards
                     if str(c.get("project") or "") == project
                     and str(c.get("queue_state") or "") == "queued"
                     and str(c.get("id") or "") != cid
                     and board_queue.queue_key(c) < mine_key]
            return False, _D._already_queued_reason(
                len(ahead) + 1, bool(self.board_autostart_enabled))
        fields = {"queue_state": "queued", "queued_at": time.time(),
                  "dispatch_error": ""}
        updated, detail = await self._board_call("update", card["id"], fields)
        if updated is None:
            # The store refused — `MAX_QUEUED_PER_PROJECT`, almost always.
            # Refused rather than queued, and said in words: a queue that
            # silently drops an enqueue is a card nobody will ever start.
            return False, detail
        await self._publish_board()
        if unmet:
            titles = [str(e.get("title") or "") for e in unmet]
            logger.info("queued card %s: waiting on %d unfinished card(s)",
                        card["id"][:8], len(titles))
            return False, _D._dependency_reason(
                titles, bool(self.board_autostart_enabled))
        project = str(card.get("project") or "")
        if active is None:
            active = self._claiming_session_ids()
        running = len(board_queue.claims(
            await self._board_call("cards") or [], project, active))
        logger.info("queued card %s: %s of %d places in %s are taken",
                    card["id"][:8], running,
                    self._parallel_limit_for(card.get("root")),
                    project or "that project")
        return False, _D._queue_reason(running, bool(self.board_autostart_enabled))

    async def _queue_hard_refusal(self, card: Optional[dict], detail: str,
                                  queued_replay: bool) -> tuple:
        """A refusal that will still refuse in an hour. `(False, detail)`.

        On a person's press this is just the refusal. On a *replay* it also
        takes the card out of the queue and writes the refusal as
        `dispatch_error` — the orange line the board already draws — because
        the queue may never hold a card it can never start, and an automatic
        action that fails must fail visibly. Without the split the drain would
        retry a doomed card at reconcile cadence for ever, which is the one
        loop this design can grow.
        """
        if queued_replay and card is not None:
            await self._board_call("update", card["id"], {
                "queue_state": "", "queued_at": None,
                "dispatch_error": detail})
            await self._publish_board()
            logger.info("dequeued card %s: %s", str(card.get("id"))[:8], detail)
            self._log_card_event(card, "card_dispatch_failed", error=detail)
        return False, detail

    async def unqueue_card(self, card_id: str) -> tuple:
        """Take a card out of its project's queue. `(ok, detail)`.

        A verb of its own for `reset_card`'s reason: `queue_state` and
        `queued_at` are Dark Army's own bookkeeping and are outside
        `ApiServer._BOARD_FIELDS`, so a surface may **clear** a queue slot and
        may never set one — a surface that could set one would be
        manufacturing a claim on Dark Army's future auto-start.

        Unarmed on every surface that offers it: clearing a queue slot
        destroys nothing and the undo is pressing Start again.
        """
        if self._board is None:
            return False, "the board is not open"
        card = await self._board_call("get", card_id)
        if card is None:
            return False, "no such card"
        if str(card.get("queue_state") or "") != "queued":
            return False, "that card is not queued"
        updated, detail = await self._board_call("update", card_id, {
            "queue_state": "", "queued_at": None, "dispatch_error": ""})
        if updated is None:
            return False, detail
        getattr(self, "_own_terminal_dispatch", set()).discard(str(card_id))
        await self._publish_board()
        return True, "taken out of the queue"

    async def move_queued_card(self, card_id: str, before_id: str) -> tuple:
        """Reorder a card already in its project's queue. `(ok, detail)`.

        A verb of its own for `unqueue_card`'s reason: `queue_rank` is
        outside `_WRITABLE` and `_BOARD_FIELDS`, so a surface may reorder
        cards already queued and may never put one in. Unarmed: a reorder
        destroys nothing and its undo is dragging back.

        Deliberately takes no `_board_write_lock` and never touches
        `_after_board_write`: it moves no column, so the wrap-up leg has
        no business here, and the race answer is the WHERE clause in
        `move_queued` itself — `review_card`'s pattern.
        """
        if self._board is None:
            return False, "the board is not open"
        card, detail = await self._board_call(
            "move_queued", card_id, before_id)
        if card is None:
            return False, detail
        await self._publish_board()
        return True, detail

    async def _flush_queue_dispatches(self) -> None:
        """Start the cards the reconcile decided may go. Runs on the loop.

        `_flush_auto_compacts`' pattern verbatim: the decision was made on the
        executor inside the snapshot push, where reading plans and cards is
        free, and the *action* — which awaits a subprocess — happens here,
        after the snapshot has gone out, so a slow spawn never holds the fleet
        picture a frame behind.

        Both switches are re-checked here as well as in the decision, so no
        surface and no future caller can route around either: `board_dispatch`
        removes the capability entirely and `board_autostart` removes only the
        drain, leaving the gate and the order intact. Twice on purpose — a
        candidate named a second before the toggle flipped must not still be
        started a second after it.
        """
        candidates, self._queue_candidates = self._queue_candidates, []
        if not candidates:
            return
        if not (self.board_dispatch_enabled and self.board_autostart_enabled):
            # Not a dequeue. With the drain off the queue is still the truth
            # about what a person asked for and what order they asked in; only
            # the press is missing.
            return
        for card_id in candidates:
            try:
                ok, detail = await self.dispatch_card(
                    card_id, queued_replay=True)
            except Exception:
                logger.warning("queue drain failed for %s", card_id[:8],
                               exc_info=True)
                continue
            if ok:
                logger.info("queue started card %s", card_id[:8])

    async def refine_card(self, card_id: str) -> tuple:
        """Dispatch a *planning* session onto a Prep card. `(ok, detail)`.

        The Prep column's verb, and `dispatch_card`'s sibling in everything
        that guards it: the `board_dispatch` preference first (Refine is Dark Army
        starting a session, and a second toggle for the same capability would
        be two copies of a gate), `_dispatch_lock` end to end for its stated
        reason (the bounds are enforced, not computed, and refinements and
        dispatches share one in-flight budget), `refine_guard` re-checked
        against the store at the moment of dispatch, and the same
        `board_own_terminal` preference deciding where the terminal opens
        (`dispatch.spawn_local` or `dispatch.spawn`) — there is no per-press
        HERE on this verb, so the preference is the whole answer.

        What it deliberately does **not** share is the aftermath. The card
        does not move, and `session_id`/`link_state` are untouched — that is
        the whole reason `refine_state`/`refine_session_id` exist as separate
        fields: `bind_session` moves a card to In progress, which a refining
        card must never do. The assistant is `card["tool"]` for all three,
        the same `/ship` family Start already sends; attach is inbound
        `dark_army_attach_plan`, which Grok and Codex can call.
        """
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        if self._board is None:
            return False, "the board is not open"
        async with self._dispatch_lock:
            return await self._refine_card_locked(card_id)

    async def _enrollment_refusal(self, card: dict) -> str:
        """Empty unless this card's project is not enrolled.

        Re-read out of the ledger at the instant of dispatch rather than taken
        off a snapshot, in the house style of `delete_abandoned_agent`'s
        category guard: a card armed while its project was enrolled must not
        start after it was un-enrolled.
        """
        root = str((card or {}).get("root") or "")
        if not root:
            return ""
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, enrollment.root_enrolled, root)
        if found:
            return ""
        return ("this project is not enrolled with Dark Army — enrol it from the ⋯ "
                "menu → Projects before starting work in it")

    async def _attach_named_plan(self, card: dict):
        """Attach the plan a Prep card's notes lead with. The card, or `None`.

        The recovery for a card filed with its plan named only in the notes
        (`dispatch.named_plan`): before this, Refine opened a planning run
        whose notes said "implement", the run built the work, and the card
        stayed in Prep for good because only an attach leaves Prep. The path
        passes `_plan_path_refusal` against the card's own root, exactly as a
        session's attach does; anything short of that answers `None` and the
        caller refines as it always did. No session is named — a person's
        press did this — and `start_when_planned` is honoured as it is on
        every attach.
        """
        path = dispatch.named_plan(card)
        if not path:
            return None
        loop = asyncio.get_running_loop()
        resolved, refusal = await loop.run_in_executor(
            None, self._plan_path_refusal, card.get("root") or "", path)
        if refusal:
            return None
        attached, _ = await self._board_call(
            "attach_plan", card["id"], resolved, "")
        if attached is None:
            return None
        attached = await self._seed_from_plan(attached, resolved)
        await self._publish_board()
        self._log_card_event(attached, "card_plan_attached",
                             plan_path=os.path.basename(resolved))
        self._schedule_auto_start(attached)
        return attached

    async def _refine_card_locked(self, card_id: str) -> tuple:
        """The body of `refine_card`, under `_dispatch_lock`. No other caller."""
        card = await self._board_call("get", card_id)
        if card is not None:
            refusal = await self._enrollment_refusal(card)
            if refusal:
                return False, refusal
        loop = asyncio.get_running_loop()
        roots = await loop.run_in_executor(None, self._known_project_roots)
        # Where the terminal opens is the `board_own_terminal` preference,
        # exactly as it is for Start. Refine has no per-press HERE, so the
        # preference is the whole answer; read once so the enrolled-root
        # widening and the spawner cannot disagree.
        use_own = bool(getattr(self, "board_own_terminal_enabled", False))
        if use_own:
            # `_dispatch_card_locked`'s widening, for its stated reason: with
            # Dark Army's own terminal on, a refinement needs no editor window, so
            # the known set is the enrolment ledger. `refine_guard` still
            # demands an exact member that is a real directory, and
            # `_enrollment_refusal` already ran above.
            enrolled = await loop.run_in_executor(None, enrollment.enrolled_roots)
            roots = set(roots) | {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
        in_flight = self._launch_inflight(
            await self._board_call("cards") or [])
        ok, detail = dispatch.refine_guard(
            card, roots=roots, in_flight=in_flight, now=time.time(),
            last_attempt=self._dispatch_attempts.get(str(card_id)))
        if not ok:
            return False, detail

        # A finished plan the notes already name is attached, not re-planned.
        # Every guard above has run, so this press could have launched a
        # planning session; it launches nothing instead. A path that fails
        # containment falls through to the ordinary refinement.
        attached = await self._attach_named_plan(card)
        if attached is not None:
            return True, "the plan its notes name is attached — the card moved to Backlog"

        tool = str(card.get("tool") or "")
        executable = await loop.run_in_executor(
            None, dispatch.resolve_executable, tool)
        if not executable:
            return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool=tool)

        # Same argv shape Start already uses (`argv_for`): grok and codex
        # get `--` in front of the prompt. The card's own model is Start's
        # alone; the planning session runs on the assistant's main-session
        # slot for this project (`_agent_model_for`), Default until chosen.
        # `refine_prompt` now carries the stored instructions, so the
        # attachment block is Start's own: strip baked-in path lines, then
        # append the resolved copies. A vanished copy is omitted rather
        # than refusing the planning session.
        prompt = dispatch.refine_prompt(card)
        rels = attachments.split_field(card.get("attachments"))
        if rels:
            abs_paths = await loop.run_in_executor(
                None, attachments.resolve_paths, rels)
            prompt = (attachments.strip_path_lines(prompt, abs_paths)
                      + attachments.prompt_block_from_abs(abs_paths))
        # Same objective Start appends, in the same place: the planning
        # session is told who benefits and what counts as success, so the
        # plan's acceptance criteria can answer the person's own criterion.
        prompt += dispatch.objective_block(card)
        argv = dispatch.argv_for(
            tool, executable, prompt,
            model=self._agent_model_for(card.get("root"), tool, "main"))
        name = ("refine: " + (card.get("title") or "card"))[:40]
        # Before the spawn await, for `_dispatch_card_locked`'s stated reason:
        # a SessionStart that beats the extension reply must not land in the
        # baseline it is measured against.
        baseline = self._live_session_ids()
        spawner = dispatch.spawn_local if use_own else dispatch.spawn
        # A refinement's stage is not a guess: this session is the planner,
        # which is what `_card_roles_by_session` already calls a live
        # `refine_state`.
        spawned, spawn_detail, shell_pid = await spawner(
            card["root"], argv, name,
            stamp=origin.stamp("card-refine", card["id"], "bc-planner"))
        if not spawned:
            return False, spawn_detail

        await self._note_refine_spawn(card, time.time(), baseline, shell_pid,
                                      use_own, batch="")
        await self._publish_board()
        logger.info("refining card %s in %s", card["id"][:8], card["root"])
        self._log_card_event(card, "card_dispatched", tool=tool,
                             root=str(card.get("root") or ""), phase="refinement")
        return True, spawn_detail

    async def _note_refine_spawn(self, card: dict, now: float, baseline: set,
                                 shell_pid, use_own: bool, *, batch: str = "",
                                 rank: int = 0) -> None:
        """Record one card's side of a refinement spawn: the cooldown stamp,
        the bind baseline, the terminal receipt and the store's
        `refine_state = "dispatching"`.

        `_refine_card_locked`'s tail, lifted out so the batch verb writes each
        of its cards exactly as a single Refine writes its one. `batch` and
        `rank` ride the store update **only when `batch` is non-empty**, so a
        single Refine's update dict is byte-identical to what it was. Every
        card of a batch gets the same baseline, receipt and stamp, which is
        what lets `_bind_refining_card` bind them all to the one session on
        the same pass without a line of its own changing.
        """
        self._dispatch_attempts[card["id"]] = now
        self._refine_baseline[card["id"]] = baseline
        # The terminal receipt, shared with the dispatch's map: the two verbs
        # are mutually exclusive on one card while either is `dispatching`
        # (each guard refuses the other's state), so at any moment exactly one
        # bind reads the receipt and it is the one whose spawn wrote it.
        # A pty spawn's third element is the child's own pid — an identity,
        # not a descent proof — so it goes in the *other* map, the same split
        # `_dispatch_card_locked` makes. Absence pops both, so a receipt from
        # an earlier press on the other path cannot outlive it.
        if use_own:
            self._spawn_shell_pids.pop(card["id"], None)
            if shell_pid:
                self._spawn_pty_pids[card["id"]] = shell_pid
            else:
                self._spawn_pty_pids.pop(card["id"], None)
        else:
            self._spawn_pty_pids.pop(card["id"], None)
            if shell_pid:
                self._spawn_shell_pids[card["id"]] = shell_pid
            else:
                self._spawn_shell_pids.pop(card["id"], None)
        # The card does not move, and `session_id`/`link_state` are untouched.
        # `dispatched_at` is reused as the bind-window clock — judged, traced
        # and accepted rather than a slip: the two verbs are mutually
        # exclusive on one card while either is `dispatching` (each guard
        # refuses the other's state, and both run under `_dispatch_lock`), so
        # at any moment exactly one bind is reading the stamp and it is the
        # one that wrote it. A refinement that `ended` can later be
        # dispatched, but by then the refine bind is over and the dispatch
        # rewrites the stamp for itself; `reset_card` clears the stamp and
        # both link fields in one write, so no interleaving hands one verb
        # the other's clock.
        fields = {
            "refine_state": "dispatching",
            "dispatched_at": now,
            "dispatch_error": "",
        }
        if batch:
            fields["batch_id"] = batch
            fields["batch_rank"] = str(rank)
        await self._board_call("update", card["id"], fields)

    async def refine_cards(self, card_ids: list) -> tuple:
        """Dispatch **one** planning session onto several Prep cards.
        `(ok, detail)`.

        `refine_card`'s sibling for a pile: the same preference, the same
        lock end to end, every card judged by `refine_guard` against the
        store at the moment of dispatch, and one spawn. Every card is then
        written exactly as a single Refine writes its one (`_note_refine_spawn`)
        plus a shared batch mark — `batch_id`, a token minted here, and
        `batch_rank`, the card's place in the prompt — which is what
        `_launch_inflight` counts once and what the attach and the reconcile
        read. Nothing about a single Refine changes.
        """
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        if self._board is None:
            return False, "the board is not open"
        async with self._dispatch_lock:
            return await self._refine_cards_locked(list(card_ids or []))

    async def _refine_cards_locked(self, card_ids: list) -> tuple:
        """The body of `refine_cards`, under `_dispatch_lock`. No other caller.

        Refuses the whole press on the first card that could not be refined
        alone, in words naming that card, and writes nothing to any card
        before the spawn has succeeded.
        """
        ids: list = []
        for raw in card_ids:
            cid = str(raw or "").strip()
            if cid and cid not in ids:
                ids.append(cid)
        if len(ids) < 2:
            return False, BATCH_TOO_FEW_REFUSAL
        if len(ids) > board.MAX_BATCH_CARDS:
            return False, BATCH_TOO_MANY_REFUSAL.format(limit=board.MAX_BATCH_CARDS)
        cards: list = []
        for cid in ids:
            card = await self._board_call("get", cid)
            if card is None:
                return False, "no such card"
            cards.append(card)

        def title_of(card: dict) -> str:
            return str(card.get("title") or "").strip() or "a card"

        for card in cards:
            refusal = await self._enrollment_refusal(card)
            if refusal:
                return False, f"{title_of(card)}: {refusal}"
        loop = asyncio.get_running_loop()
        card_roots = await loop.run_in_executor(
            None, lambda: [dispatch.normalise_root(c.get("root") or "")
                           for c in cards])
        for card, croot in zip(cards, card_roots):
            if croot != card_roots[0]:
                return False, BATCH_MIXED_ROOT_REFUSAL.format(title=title_of(card))
        tool = str(cards[0].get("tool") or "")
        for card in cards:
            if str(card.get("tool") or "") != tool:
                return False, BATCH_MIXED_TOOL_REFUSAL.format(title=title_of(card))
        for card in cards:
            if dispatch.named_plan(card):
                return False, BATCH_NAMED_PLAN_REFUSAL.format(title=title_of(card))

        # Computed once for the whole press, `_refine_card_locked`'s widening
        # verbatim: the cards share one root, so they share one answer.
        roots = await loop.run_in_executor(None, self._known_project_roots)
        use_own = bool(getattr(self, "board_own_terminal_enabled", False))
        if use_own:
            enrolled = await loop.run_in_executor(None, enrollment.enrolled_roots)
            roots = set(roots) | {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
        in_flight = self._launch_inflight(
            await self._board_call("cards") or [])
        now = time.time()
        for card in cards:
            ok, detail = dispatch.refine_guard(
                card, roots=roots, in_flight=in_flight, now=now,
                last_attempt=self._dispatch_attempts.get(str(card["id"])))
            if not ok:
                return False, f"{title_of(card)}: {detail}"

        # One prompt: the batch head and a block per card, then each card's
        # attachments under a line naming its place. The batch prompt bakes
        # in no path lines, so nothing is stripped; a vanished copy is
        # omitted, as a single Refine omits it.
        prompt = dispatch.refine_batch_prompt(cards)
        for k, card in enumerate(cards, start=1):
            rels = attachments.split_field(card.get("attachments"))
            if not rels:
                continue
            abs_paths = await loop.run_in_executor(
                None, attachments.resolve_paths, rels)
            block = attachments.prompt_block_from_abs(abs_paths)
            if block:
                prompt += f"\n\nAttachments for card {k}:" + block
        # Belt and braces, as `refine_guard` does for one card: the head is
        # Dark Army's own, but the check costs nothing and belongs here.
        refusal = dispatch.prompt_refusal(tool, prompt)
        if refusal:
            return False, refusal
        executable = await loop.run_in_executor(
            None, dispatch.resolve_executable, tool)
        if not executable:
            return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool=tool)
        root = str(cards[0].get("root") or "")
        argv = dispatch.argv_for(
            tool, executable, prompt,
            model=self._agent_model_for(root, tool, "main"))
        n = len(cards)
        name = (f"refine: {n} cards")[:40]
        # Before the spawn await, `_refine_card_locked`'s stated reason.
        baseline = self._live_session_ids()
        spawner = dispatch.spawn_local if use_own else dispatch.spawn
        # The stamp names one card — the first — because an origin names one;
        # the others reach the session through `refine_session_id`.
        spawned, spawn_detail, shell_pid = await spawner(
            root, argv, name,
            stamp=origin.stamp("card-refine", cards[0]["id"], "bc-planner"))
        if not spawned:
            return False, spawn_detail

        token = secrets.token_hex(8)
        now = time.time()
        for k, card in enumerate(cards, start=1):
            await self._note_refine_spawn(card, now, baseline, shell_pid,
                                          use_own, batch=token, rank=k)
        await self._publish_board()
        logger.info("refining %d cards in one session in %s", n, root)
        for card in cards:
            self._log_card_event(card, "card_dispatched", tool=tool,
                                 root=str(card.get("root") or ""),
                                 phase="refinement", batch=n)
        return True, spawn_detail

    # --- Batch implementation: several Backlog cards, one session ----------
    # `plans/2026-09-25-batch-implement-backlog-cards.md`.

    async def start_cards(self, card_ids: list) -> tuple:
        """Start **one** implementation session on several planned Backlog
        cards, worked one at a time. `(ok, detail)`.

        `start_project`'s precedent, one gesture on: the person ticked every
        member at the press, so Dark Army selects no work. Every card is
        judged against every rule a single Start uses — the plan gate (no
        `allow_unplanned`: a confirmation is a thing a person gives about
        one card), the enrolment refusal, `dispatch.guard`, the dependency
        gate — and a card that fails is **skipped and reported**, never
        written. Then the slot gate for the project, and one spawn through
        `_dispatch_card_locked(head, batch=…)`. The session is bound to the
        first card alone; the rest wait in Backlog carrying only `batch_id`
        / `batch_rank` until the session's own `dark_army_next_card` binds
        the next. One bound card at a time is one claim, so the batch
        counts once against the project's parallel limit by construction.

        `_dispatch_lock` is taken once for the whole press. No event-log
        kind: the head's `card_dispatched` is `_dispatch_card_locked`'s, and
        a waiting card writes no diary line.
        """
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        if self._board is None:
            return False, "the board is not open"
        async with self._dispatch_lock:
            return await self._start_cards_locked(list(card_ids or []))

    async def _start_cards_locked(self, card_ids: list) -> tuple:
        """The body of `start_cards`, under `_dispatch_lock`. No other caller.

        Whole-press refusals (too few, too many, two projects, two
        assistants, grok, no free place) write nothing; per-card refusals
        skip that card and are named in the reply. Nothing is written to any
        card before the spawn has succeeded.
        """
        ids: list = []
        for raw in card_ids:
            cid = str(raw or "").strip()
            if cid and cid not in ids:
                ids.append(cid)
        if len(ids) < 2:
            return False, BATCH_START_TOO_FEW_REFUSAL
        if len(ids) > board.MAX_BATCH_CARDS:
            return False, BATCH_START_TOO_MANY_REFUSAL.format(
                limit=board.MAX_BATCH_CARDS)

        def title_of(card: dict) -> str:
            return str(card.get("title") or "").strip() or "a card"

        # In the board's own order (`CARD_ORDER_SQL`), never tick order: the
        # shared selection is a set, and the line the session works through
        # is the line the person is looking at.
        backlog = await self._board_call("cards", ["backlog"]) or []
        wanted = set(ids)
        named = [c for c in backlog if str(c.get("id") or "") in wanted]
        skips: list = []
        found = {str(c.get("id") or "") for c in named}
        for cid in ids:
            if cid in found:
                continue
            gone = await self._board_call("get", cid)
            skips.append((title_of(gone or {}), BATCH_NOT_BACKLOG_REFUSAL))
        if len(named) < 2:
            return False, _skip_lines(BATCH_START_TOO_FEW_REFUSAL, skips)

        loop = asyncio.get_running_loop()
        card_roots = await loop.run_in_executor(
            None, lambda: [dispatch.normalise_root(c.get("root") or "")
                           for c in named])
        for card, croot in zip(named, card_roots):
            if croot != card_roots[0]:
                return False, BATCH_START_MIXED_ROOT_REFUSAL.format(
                    title=title_of(card))
        tool = str(named[0].get("tool") or "")
        for card in named:
            if str(card.get("tool") or "") != tool:
                return False, BATCH_START_MIXED_TOOL_REFUSAL.format(
                    title=title_of(card))
        if tool == "grok":
            return False, BATCH_TOOL_REFUSAL

        # Computed **once, before any spawn**, so members 2..n are judged
        # against the board as it stood at the press — never refused by the
        # head's own launch (`PROJECT_BUSY_REFUSAL`).
        roots = await loop.run_in_executor(None, self._known_project_roots)
        if bool(getattr(self, "board_own_terminal_enabled", False)):
            enrolled = await loop.run_in_executor(None, enrollment.enrolled_roots)
            roots = set(roots) | {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
        all_cards = await self._board_call("cards") or []
        in_flight = self._launch_inflight(all_cards)
        active = await loop.run_in_executor(None, self._claiming_session_ids)
        running_ids = self._board_running_ids(self._agents_snapshot_cache or {})
        by_id = {c["id"]: c for c in all_cards if c.get("id")}
        now = time.time()
        survivors: list = []
        for card in named:
            refusal = ""
            if str(card.get("kind") or "") == board.KIND_SCOUT:
                refusal = BATCH_SCOUT_REFUSAL
            elif str(card.get("queue_state") or "") == "queued":
                refusal = BATCH_QUEUED_REFUSAL
            elif str(card.get("batch_id") or ""):
                refusal = BATCH_WAITING_REFUSAL
            if not refusal:
                refusal = await self._plan_gate_refusal(card)
            if not refusal:
                refusal = await self._enrollment_refusal(card)
            if not refusal:
                ok, detail = dispatch.guard(
                    card, roots=roots, in_flight=in_flight, now=now,
                    last_attempt=self._dispatch_attempts.get(str(card["id"])))
                # A transient refusal is a skip too, never an enqueue: a
                # queue of a batch is not a thing the drain can start.
                refusal = "" if ok else detail
            if not refusal:
                earlier = {str(c.get("id") or "") for c in survivors}
                unmet = await loop.run_in_executor(
                    None, functools.partial(
                        self._unmet_dependencies, card, by_id,
                        running_ids, active))
                # A card waiting on an earlier member of this same batch is
                # fine: the session reaches it only after that one.
                if [e for e in unmet if e.get("id") not in earlier]:
                    refusal = BATCH_DEPENDENCY_REFUSAL
            if refusal:
                skips.append((title_of(card), refusal))
            else:
                survivors.append(card)
        if len(survivors) < 2:
            return False, _skip_lines(BATCH_START_TOO_FEW_REFUSAL, skips)

        head = survivors[0]
        held = await loop.run_in_executor(
            None, functools.partial(
                self._slot_refusal, head, all_cards, active=active,
                running_ids=running_ids))
        if held:
            return False, BATCH_NO_PLACE_REFUSAL.format(
                project=str(head.get("project") or "this project"))

        ok, detail = await self._dispatch_card_locked(
            str(head["id"]), batch=survivors)
        if not ok:
            return False, _skip_lines(detail, skips)
        token = str(getattr(self, "_last_batch_token", "") or "")
        if not token and detail == worktrees.PREPARING_NOTE:
            # The batch's one worktree is being prepared: nothing has
            # spawned and nothing is marked. The prepare task re-enters
            # `start_cards` with these same cards once the folder is ready,
            # and every rung above runs again at that instant.
            return True, _skip_lines(detail, skips)
        waiting = 0
        for rank, card in enumerate(survivors[1:], start=2):
            if not token:
                break
            # `bump=False`: Dark Army's own bookkeeping, not a person's edit.
            # The column stays `backlog`; only the mark is written.
            marked, _why = await self._board_call("update", card["id"], {
                "batch_id": token, "batch_rank": str(rank),
                "dispatch_error": ""}, bump=False) or (None, "")
            if marked is not None:
                waiting += 1
        await self._publish_board()
        logger.info("started %d cards in one session in %s (%d skipped)",
                    waiting + 1, card_roots[0], len(skips))
        return True, _D._start_batch_report(title_of(head), waiting, skips)

    async def _implement_batch_prompt(self, cards: list) -> str:
        """`dispatch.implement_batch_prompt(cards)` plus what the daemon
        appends: each member's attachments under a line naming its place,
        then the area block once per distinct area. The batch prompt bakes
        in no path lines, so nothing is stripped; a vanished copy is
        omitted, as a single Start omits it. Loop; the file reads hop."""
        loop = asyncio.get_running_loop()
        prompt = dispatch.implement_batch_prompt(cards)
        for k, card in enumerate(cards, start=1):
            rels = attachments.split_field(card.get("attachments"))
            if not rels:
                continue
            abs_paths = await loop.run_in_executor(
                None, attachments.resolve_paths, rels)
            block = attachments.prompt_block_from_abs(abs_paths)
            if block:
                prompt += f"\n\nAttachments for card {k}:" + block
        seen_areas: set = set()
        for card in cards:
            slug = str(card.get("area") or "")
            if not slug or slug in seen_areas:
                continue
            seen_areas.add(slug)
            brief = areas.brief_path(slug)
            brief_present = bool(brief) and await loop.run_in_executor(
                None, os.path.isfile,
                os.path.join(str(card.get("root") or ""), brief))
            prompt += dispatch.area_block(card, brief_present)
        return prompt

    async def advance_batch_by_session(self, session_id: str) -> tuple:
        """Move a batch session on to its next waiting card.
        `(ok, detail, card_or_None)`.

        The card is resolved from the session and never named by the
        caller — `close_card_by_session`'s scope. The card it is on now, if
        still open, is **left**: marked `ended` with its session kept (so
        it reads as finished-but-not-closed work, `_consider_work_record`
        and `_freeze_run_health` run at this seam as at the reconcile's),
        and the next waiting member in rank order is bound to the session
        (`bind_session`, In progress, `live`). Each candidate re-runs the
        per-card rungs — the vanished-plan rung of the plan gate, the
        enrolment refusal, `dispatch.guard` with no launch bounds (nothing
        spawns here), the dependency gate — and one that fails leaves the
        batch with its refusal on it. **Nothing starts**: the person chose
        every member at the press, and this binds one of them to a session
        that is already running.
        """
        if self._board is None:
            return False, "the board is not open", None
        if not session_id:
            return False, BATCH_NOT_BOUND_REFUSAL, None
        if not self.board_dispatch_enabled:
            # The launcher switch removes this too: a card moving into In
            # progress under a session is work Dark Army put in front of it.
            return (False,
                    "Dark Army is not allowed to start sessions (see the ⋯ menu)",
                    None)
        async with self._dispatch_lock:
            return await self._advance_batch_locked(session_id)

    async def _advance_batch_locked(self, session_id: str) -> tuple:
        """The body of `advance_batch_by_session`, under `_dispatch_lock`."""
        mine = await self._board_call("by_session", session_id) or []
        marks = {str(c.get("batch_id") or "") for c in mine}
        if not mine or len(marks) != 1 or "" in marks:
            return False, BATCH_NO_BATCH_REFUSAL, None
        bid = marks.pop()
        members = await self._board_call("batch_members", bid) or []
        # Only the batch's owning session may walk it (`_batch_owner`): the
        # session the press bound, whether or not its first card still
        # exists. A session that came by a member some other way (a card
        # dragged out of Backlog and forced onto it) is not this batch's,
        # and must never take its next card.
        owner = _batch_owner(members)
        if owner is None or str(owner.get("session_id") or "") != session_id:
            return False, BATCH_NO_BATCH_REFUSAL, None
        current = [m for m in members
                   if str(m.get("session_id") or "") == session_id
                   and str(m.get("column_name") or "") != "done"
                   and str(m.get("link_state") or "") != "ended"]
        if len(current) > 1:
            return False, BATCH_BUSY_REFUSAL, None
        loop = asyncio.get_running_loop()
        now = time.time()
        if current:
            left = current[0]
            await self._board_call("mark_ended", left["id"], when=now)
            snapshot = self._agents_snapshot_cache or {}
            await loop.run_in_executor(
                None, self._consider_work_record, left, snapshot)
            await self._collect_work_record_now(str(left["id"]))
            await loop.run_in_executor(
                None, self._freeze_run_health, left, snapshot)
        else:
            # The card this session has just closed keeps `live` in Done, so
            # the reconcile would record it only when the whole session ends
            # — with the last card's report and a file list covering every
            # card after it. Record it here, off the report the session
            # printed for it, before the next card is bound.
            # `_consider_work_record` is idempotent on the run, so the
            # session's end adds no second record.
            closed = [m for m in members
                      if str(m.get("session_id") or "") == session_id
                      and str(m.get("column_name") or "") == "done"
                      and str(m.get("closed_by") or "") == session_id]
            if closed:
                newest = max(closed,
                             key=lambda m: float(m.get("done_at") or 0.0))
                snapshot = self._agents_snapshot_cache or {}
                await loop.run_in_executor(
                    None, self._consider_work_record, newest, snapshot)
                await self._collect_work_record_now(str(newest["id"]))
                await loop.run_in_executor(
                    None, self._freeze_run_health, newest, snapshot)
        reached = max([_batch_rank(m) for m in members
                       if str(m.get("session_id") or "") == session_id] or [0])
        size = max([len(members)] + [_batch_rank(m) for m in members])
        waiting = sorted(
            [m for m in members
             if _batch_waiting(m) and _batch_rank(m) > reached],
            key=_batch_rank)
        roots = None
        active = None
        for member in waiting:
            if roots is None:
                roots = await loop.run_in_executor(
                    None, self._known_project_roots)
                if bool(getattr(self, "board_own_terminal_enabled", False)):
                    enrolled = await loop.run_in_executor(
                        None, enrollment.enrolled_roots)
                    roots = set(roots) | {dispatch.normalise_root(r)
                                          for r in enrolled if r}
                    roots.discard("")
                active = await loop.run_in_executor(
                    None, self._claiming_session_ids)
            refusal = await self._plan_gate_refusal(member, replay=True)
            if not refusal:
                refusal = await self._enrollment_refusal(member)
            if not refusal:
                # `in_flight=[]`: the advance spawns nothing, so the launch
                # bounds do not apply; every per-card rung still does.
                ok, detail = dispatch.guard(member, roots=roots, in_flight=[],
                                            now=now, last_attempt=None)
                refusal = "" if ok else detail
            if not refusal:
                unmet = await loop.run_in_executor(
                    None, functools.partial(
                        self._unmet_dependencies, member, {},
                        None, active))
                if unmet:
                    refusal = BATCH_DEPENDENCY_REFUSAL
            if refusal:
                await self._board_call("update", member["id"], {
                    "dispatch_error": refusal, "batch_id": "",
                    "batch_rank": ""}, bump=False)
                continue
            # Re-read and bind under one store lock: a drag or reset that
            # landed during the hops above leaves the card alone.
            bound, _why = await self._board_call(
                "bind_waiting_member", member["id"], session_id,
                bid) or (None, "")
            if bound is None:
                continue
            bound, _why = await self._board_call("update", member["id"], {
                "dispatched_at": now, "dispatch_error": ""},
                bump=False) or (bound, "")
            await loop.run_in_executor(
                None, functools.partial(
                    self._record_outcome_binding, bound, session_id,
                    "implementation", late=False))
            # The batch shares one folder and one branch — the head's
            # (`docs/card-worktrees.md`): carried onto this card as it is
            # bound, so its record, its release and its baseline are the
            # worktree's, not the main checkout's.
            shared = next((m for m in sorted(members, key=_batch_rank)
                           if str(m.get("worktree_path") or "")
                           and str(m.get("worktree_branch") or "")), None)
            folder = str(member.get("root") or "")
            if shared is not None:
                carried, _why = await self._board_call(
                    "record_worktree", member["id"],
                    str(shared["worktree_path"]),
                    str(shared["worktree_branch"])) or (None, "")
                if carried is not None:
                    bound = carried
                    folder = str(shared["worktree_path"])
            self._schedule_work_baseline(str(member["id"]), folder, now)
            await self._publish_board()
            rank = _batch_rank(bound or member)
            logger.info("batch session %s moved on to card %s (%d of %d)",
                        session_id[:12], str(member["id"])[:8], rank, size)
            out = dict(bound or member)
            out["batch_size"] = size
            return True, f"now on card {rank} of {size}", out
        await self._publish_board()
        return True, "the batch is finished — no card left", None

    async def _handle_board_next_request(self, msg: dict) -> dict:
        """`dark_army_next_card`, through Dark Army's channel.

        `_handle_board_close_request`'s scope discipline: the message
        carries **no card id** and the schema has no property for one; the
        caller is resolved from the port (`_board_request_session_fresh`,
        the close verb's attribution — this verb is open to Codex and it
        moves a card). The port is the addressing and the scope is the
        defence: a forger who reaches it can only move *this* session on to
        a card the person already chose for it at the press. A card id rides
        the **reply**, the close verb's existing shape; none is accepted on
        the request.
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": BATCH_NOT_BOUND_REFUSAL}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        ok, detail, card = await self.advance_batch_by_session(session_id)
        if not ok:
            return {"ok": False, "detail": detail}
        if card is None:
            return {"ok": True, "detail": detail, "remaining": 0}
        return {"ok": True, "detail": detail, "card_id": card.get("id"),
                "title": card.get("title"),
                "plan_path": card.get("plan_path"),
                "rank": _batch_rank(card),
                "size": int(card.get("batch_size") or 0)}

    async def ask_card(self, card_id: str, text: str) -> tuple:
        """Ask a question about a card. `(ok, detail)`.

        Direct when the bound session is a live claude channel: a `kind="user"`
        push, recorded only if the push returned True. Otherwise a helper
        session, behind `board_dispatch` — a helper is a launch, so the same
        one-place refusal `dispatch_card` uses. Never touches the card's
        `session_id`, `link_state`, `column_name` or `dispatch_error`.
        """
        if self._board is None:
            return False, "the board is not open"
        text = str(text or "").strip()
        if not text:
            return False, "a question needs some text"
        if len(text) > board.MAX_MESSAGE_CHARS:
            return False, (f"that question is longer than "
                           f"{board.MAX_MESSAGE_CHARS} characters")
        card = await self._board_call("get", card_id)
        if card is None:
            return False, "no such card"

        sid = str(card.get("session_id") or "")
        provider = self._session_provider(sid) or "claude"
        channel = (self._channel_for_session(sid)
                   if card.get("link_state") == "live" and sid
                   and provider == "claude" else None)
        if channel is not None:
            # The tail names the tool this session was born with: the
            # channel's registered name, kept on its registry entry.
            content = text + "\n\n" + ask_direct_tail(channel.get("name"))
            pushed = await self.push_channel_event(
                sid, content, {"kind": "user"})
            if not pushed:
                return False, "That session is no longer listening."
            msg, detail = await self._board_call(
                "add_message", card["id"], "user", text, "question", "")
            if msg is None:
                return False, detail
            await self._publish_board()
            return True, "asked"

        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        async with self._dispatch_lock:
            return await self._ask_card_consult_locked(card, text)

    async def _ask_card_consult_locked(self, card: dict, text: str) -> tuple:
        """The consultant half of `ask_card`, under `_dispatch_lock`."""
        cid = str(card.get("id") or "")
        # Bound helpers are out of `_launch_inflight` (they have a session
        # and no longer occupy the launch bound) but still block a second
        # Ask on *this* card. Unbound rows are also caught by
        # `consult_guard`'s `consult:<id>` check.
        if cid in self._consults:
            return False, "a helper is already looking at this card"
        loop = asyncio.get_running_loop()
        roots = await loop.run_in_executor(None, self._known_project_roots)
        in_flight = self._launch_inflight(
            await self._board_call("cards") or [])
        ok, detail = dispatch.consult_guard(
            card, roots=roots, in_flight=in_flight, now=time.time(),
            last_attempt=self._consult_attempts.get(cid),
            question=text)
        if not ok:
            return False, detail
        executable = await loop.run_in_executor(
            None, dispatch.resolve_executable, "claude")
        if not executable:
            return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool="claude")
        brief = dispatch.consult_brief(card, text)
        # A consult is always claude, so it takes claude's main-session slot
        # for this project — the same seam as Start and Refine.
        argv = dispatch.argv_for(
            "claude", executable, brief,
            model=self._agent_model_for(card.get("root"), "claude", "main"))
        name = ("ask: " + (card.get("title") or "card"))[:40]
        # Before the spawn await, for `_dispatch_card_locked`'s stated reason:
        # a SessionStart that beats the extension reply must not land in the
        # baseline it is measured against.
        baseline = self._live_session_ids()
        # A consult is an equally unexplained session, so it says so too. No
        # stage: nobody is doing a stage of the card's work, which is the
        # whole point of a consult.
        spawned, spawn_detail, shell_pid = await dispatch.spawn(
            card["root"], argv, name,
            stamp=origin.stamp("card-consult", cid))
        if not spawned:
            return False, spawn_detail
        now = time.time()
        self._consult_attempts[cid] = now
        self._consults[cid] = {
            "card_id": cid,
            "project": card.get("project") or "",
            "root": card.get("root") or "",
            "tool": "claude",
            "baseline": baseline,
            "started": now,
            "session_id": "",
            # On the consult entry, never `_spawn_shell_pids[card_id]` —
            # that map is the dispatch/refine receipt for the card itself.
            "shell_pid": shell_pid,
        }
        msg, detail = await self._board_call(
            "add_message", cid, "user", text, "question", "")
        if msg is None:
            self._consults.pop(cid, None)
            return False, detail
        await self._publish_board()
        logger.info("consulting card %s in %s", cid[:8], card["root"])
        return True, spawn_detail

    async def message_card(self, card_id: str, text: str) -> tuple:
        """Type a person's own message onto the input line of the session
        working this card. `(ok, detail)`.

        `ask_card`'s sibling and deliberately not a widening of it. `ask_card`
        reaches a session through its *channel*, and a board-dispatched
        session is an ordinary session with no channel — which is exactly the
        population this verb exists for. So the bytes go the way `/compact`,
        `/clear` and `/low-priority` go: `session_io.send_text` onto the
        client's own input line, behind `INPUT_LINE_CLEAR`. **Never** the
        channel push `ask_card` uses — it would be dropped in silence, and
        `test_card_message.py` greps this function's source to keep it so.

        Card-scoped rather than session-scoped, and that is the capability
        argument rather than a convenience. Session-scoped this would be "Dark Army
        may type arbitrary text at any session it can see", which no verb in
        this daemon does today. Card-scoped, the reach is exactly "a session
        Dark Army itself started for this card, while that card's link is live".
        Neither surface passes a session id.

        Eight refusals, in the order the code runs them, each its own sentence
        because a person turned down needs to know which one it was: an empty
        box, a message that would run as a command, a message of more than one
        line, a message over `MAX_REPLY_CHARS`, nothing working the card, an
        assistant Dark Army cannot type at, a permission prompt already on that
        terminal, and a *question* already on that terminal.

        Four of them are not tidiness. `send_text` appends a newline, so an
        embedded one submits the head of the message at a prompt that has
        already moved on; a leading `/` is expanded by the client before any
        request exists (`/clear` typed here would end the conversation); an
        open permission dialog reads that trailing newline as confirming the
        highlighted choice; and an `AskUserQuestion` dialog owns the input line
        the same way, which is why *both* dialog gates are on this verb and on
        `can_message` — the prompt one off `_prompts_by_session()`, the
        question one off the live `_pending_questions` (with the published
        `_questions` beside it), because the broker deliberately never holds a
        question and so the first can never cover the second.
        """
        if self._board is None:
            return False, "the board is not open"
        text = str(text or "").strip()
        if not text:
            return False, _D.CARD_MESSAGE_EMPTY_REFUSAL
        if text.startswith("/"):
            return False, _D.CARD_MESSAGE_SLASH_REFUSAL
        # One test for every control character, not just `\n`: `\r` submits on
        # some clients and a `\t` moves the focus off the input line, and the
        # tail of either lands somewhere nobody chose. `0x7f` (DEL) is in the
        # test too — it sits *above* the C0 block but the client reads it as a
        # backspace, so it edits the line rather than being read on it.
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            return False, _D.CARD_MESSAGE_ONE_LINE_REFUSAL
        if len(text) > self.MAX_REPLY_CHARS:
            return False, self.CARD_MESSAGE_TOO_LONG_REFUSAL

        card = await self._board_call("get", card_id)
        if card is None:
            return False, "no such card"
        sid = str(card.get("session_id") or "")
        if not sid or card.get("link_state") != "live":
            return False, _D.CARD_MESSAGE_NO_SESSION_REFUSAL
        if self._session_provider(sid) == "codex":
            return False, _D.CARD_MESSAGE_CODEX_REFUSAL
        if sid in self._prompts_by_session():
            return False, _D.PROMPT_BLOCKED_REFUSAL
        # The prompt gate's twin, and not a widening of it. An
        # `AskUserQuestion` dialog never reaches `_prompts_by_session()` — the
        # hook broker refuses to hold one, because the question flow owns that
        # dialog — but it owns the input line every bit as hard: Dark Army's own
        # `_type_answer_burst` commits a choice by exactly this route.
        #
        # Both readings, and the live one is the load-bearing half.
        # `self._questions` is written in exactly one place,
        # `_enrich_agent_stubs`, i.e. on the agents-push cycle; a `PreToolUse`
        # that produces no state change (a session already `waiting` from an
        # earlier `idle_prompt`) schedules no push, so that cache can be up to
        # `SNAPSHOT_REFRESH_SECONDS` stale while the dialog is up.
        # `self._pending_questions` is written by that hook the instant the
        # dialog opens. Reading both can only ever *refuse* more:
        # `_pending_questions` is cleared on `QUESTION_CLEARING_EVENTS`, so a
        # stale entry costs a refusal, never a permitted press.
        #
        # `can_message` needs no such pair: `_enrich_agent_stubs` fills
        # `entry["question"]` from `_pending_questions` in the same pass it
        # writes the flag, so the flag is already reading the live source.
        # Do not "tidy" this asymmetry away by dropping either term here.
        if self._questions.get(sid) or self._pending_questions.get(sid):
            return False, _D.CARD_MESSAGE_QUESTION_REFUSAL

        pid = None
        st = self._session_states.get(sid)
        if st:
            pid = self._ensure_session_pid(sid, st)
        if pid is None:
            pid = self._roster_pid(sid)
        if not pid:
            return False, _D.CARD_MESSAGE_NO_PID_REFUSAL

        result = await session_io.send_text(
            pid, "", _D.INPUT_LINE_CLEAR + text)
        if not (result and result.get("sent")):
            return False, (_D._typed_nothing_refusal(result) or _D.NO_TYPING_WINDOW_REFUSAL)

        where = (result.get("terminalName") or result.get("matchedBy")
                 or "terminal")
        logger.info("messaged card %s session %s via %s (%d chars)",
                    str(card.get("id") or "")[:8], sid[:12], where, len(text))

        # Recorded only now, `ask_card`'s own ordering and for its reason: a
        # row written before the keystrokes landed would leave the card
        # claiming a message that never reached the terminal.
        msg, detail = await self._board_call(
            "add_message", card["id"], "user", text, "question", "terminal")
        if msg is None:
            # The act happened. A `False` here would read as "nothing was
            # sent" and invite a second press that typed it twice, so the
            # only honest answer is success with the shortfall stated.
            return True, ("Typed into the terminal, but not recorded on the "
                          f"card: {detail}")
        await self._publish_board()
        return True, "sent"

    def _launch_inflight(self, all_cards: list) -> list:
        """Dispatching cards plus *unbound* consult and ad-hoc pseudo-entries.

        Both directions of the one-per-project bound: `guard` sees consults
        that are still waiting for a session, `consult_guard` sees
        dispatching (and refining) cards. A consult that has already bound
        stays in `_consults` for attribution and does **not** occupy the
        launch bound — a helper that never answers must not park Start.

        The third kind is `open_adhoc_terminal`'s: an assistant started with
        no card at all. It is the same shape and it is here for the same
        reason — binding is by elimination, so "the first new session in this
        project" has to name one launch, whichever verb produced it. Both
        directions again: `guard` refuses a card start while an ad-hoc
        terminal is still binding (transiently, so the card queues), and
        `adhoc_guard` refuses the button while a card is `dispatching`.

        **The sweep lives here, not on a timer**, and that is load-bearing:
        `self._adhoc_launches` is read nowhere else, so an entry that leaked
        would park every card start in its project for good. An entry is
        spent when its terminal has gone, when the terminal has been named
        after a session (the bind happened — `_pty_handle_for` does it with
        no card involved), or when the bind window has run out. The test is a
        dict lookup on the handle recorded at spawn time, never a process
        walk: `ptyhost.owns` here would put a `psutil.parents()` walk on the
        loop on every dispatch.
        """
        now = time.time()
        # **A card's entry is bounded by the same window the ad-hoc entries
        # are**, and for the same reason one rung further out. The binders'
        # expiry is what normally clears `dispatching`, and it runs inside
        # `_reconcile_board` — so a reconcile that stops (a raise on that
        # path is logged and swallowed, and the machine can sleep through
        # any number of passes) used to leave one card holding its whole
        # project's launch slot for ever, refusing Start, Refine and
        # + TERMINAL alike in `PROJECT_BUSY_REFUSAL`'s transient words. Past
        # the window nobody is waiting for that session any more, whether or
        # not anything has got round to writing the card, so it no longer
        # names a launch here. The stamp is read exactly as the binders read
        # it, missing meaning 0.0 and therefore expired.
        in_flight = [c for c in all_cards
                     if (c.get("link_state") == "dispatching"
                         or c.get("refine_state") == "dispatching")
                     and now - float(c.get("dispatched_at") or 0.0)
                     <= dispatch.DISPATCH_BIND_WINDOW]
        # **One session is one launch, however many cards it was handed.** A
        # batch press marks every card it handed one session with the same
        # `batch_id`; counting each would spend the machine-wide bound on a
        # single terminal. The first card of each batch stands for it, and a
        # card with no mark is keyed on its own id — so a board with no batch
        # yields exactly the list above, in the same order.
        if any(c.get("batch_id") for c in in_flight):
            launches: dict = {}
            for c in in_flight:
                key = ("batch", str(c.get("batch_id"))) if c.get("batch_id") \
                    else ("card", str(c.get("id") or ""))
                launches.setdefault(key, c)
            in_flight = list(launches.values())
        for cid, entry in self._consults.items():
            if entry.get("session_id"):
                continue
            in_flight.append({
                "id": f"consult:{cid}",
                "project": entry.get("project") or "",
            })
        # A fix helper started in a Done card's folder
        # (`docs/card-worktrees.md`, *Review and merge*): a launch like the
        # consult's, listed while it can still be binding — the bind window,
        # then the session itself is what `_session_inside` sees.
        for cid, entry in list(
                (getattr(self, "_merge_helpers", None) or {}).items()):
            if (entry or {}).get("kind") != "fix":
                continue
            if now - float(entry.get("since") or 0.0) \
                    > dispatch.DISPATCH_BIND_WINDOW:
                continue
            in_flight.append({
                "id": f"merge-fix:{cid}",
                "project": str(entry.get("project") or ""),
            })
        for lid, entry in list(self._adhoc_launches.items()):
            handle = str(entry.get("handle") or "")
            spent = now - float(entry.get("at") or 0.0) \
                > dispatch.DISPATCH_BIND_WINDOW
            if handle and not spent:
                term = self._pty.get(handle)
                # Gone, or already named after a session: either way the
                # race this entry guards is over.
                spent = term is None or bool(term.session_id)
            # An entry with **no** handle is held for the whole window
            # rather than swept at once. `spawn_local` returning a pid
            # `ptyhost.owns` cannot place is not evidence that nothing
            # started — and sweeping on it would drop the bound at exactly
            # the moment Dark Army is least sure what it just opened. Bounded
            # either way, which is the property that matters.
            if spent:
                self._adhoc_launches.pop(lid, None)
                continue
            in_flight.append({
                "id": f"adhoc:{lid}",
                "project": entry.get("project") or "",
            })
        # The fourth kind: a card whose own branch and folder Dark Army is
        # still preparing (`docs/card-worktrees.md`). It is not `dispatching`
        # yet — nothing has spawned — but the terminal it will open is this
        # project's next launch, so another card of the project waits in
        # `PROJECT_BUSY_REFUSAL`'s transient words (and queues) meanwhile.
        # Keyed on the card's own id, so `guard` never refuses the card
        # itself. The map is replaced, never mutated, on the loop, and the
        # prepare task's `finally` always pops its entry: this is the sweep.
        for cid, entry in list(
                (getattr(self, "_worktree_preparing", None) or {}).items()):
            in_flight.append({
                "id": str(cid),
                "project": str((entry or {}).get("project") or ""),
            })
        # The fifth kind: a review run whose terminal is still binding
        # (`daemon_review`). `_adhoc_launches`' shape and reason, swept in
        # `_review_inflight_entries` the same way.
        in_flight.extend(self._review_inflight_entries())
        return in_flight

    async def open_adhoc_terminal(self, root: str, tool: str) -> tuple:
        """Start `tool` in `root` on a terminal Dark Army owns, with **no card**.
        `(ok, detail)`.

        `dispatch_card`'s sibling for the one gesture that has no paperwork
        behind it: + TERMINAL on the Agents rail. What it produces is an
        ordinary session — real session id, real hooks, its own row, its own
        Terminal tab — because nothing about a session depends on a card. The
        pty is named after the session by `_pty_handle_for` on the next
        enrich, which asks `ptyhost.owns` and `_bindable_candidate` and knows
        nothing about the board; this verb therefore adds **no binder**.

        It writes **no card, no board row and no event-log kind**: the
        ordinary `session_start` diary line already says an assistant
        started, and a record a person never asked for is paperwork by
        another name.

        Under `_dispatch_lock` for its whole body — the same lock
        `_dispatch_card_locked` holds — which is what makes the
        one-per-project launch bound real *across* the two verbs rather than
        within each of them.

        The offer set is the enrolment ledger rather than
        `_known_project_roots()`, and deliberately: this terminal is always
        Dark Army's own pty, so it needs no open editor window, exactly as Start
        widens to the ledger when the terminal is Dark Army's
        (`_dispatch_card_locked`). The ledger is re-read here, under the
        lock, so a folder chosen while enrolled and pressed after
        un-enrolment is refused at the press.
        """
        if not self.board_dispatch_enabled:
            # `dispatch_card`'s own first line, and the same switch: this is
            # Dark Army-as-launcher, and "off" has to mean off for every route to
            # it, not just for the button on a card.
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        async with self._dispatch_lock:
            loop = asyncio.get_running_loop()
            # One spelling of the folder from here down. The guard judges, the
            # label is resolved and the pty is started against the *same*
            # canonical path, so the value that passed the exact-membership
            # test against the ledger is the value the assistant is chdir'd
            # into — a caller sending `~/proj` or `/a/proj/../proj`, or a
            # symlink component swapped between the check and the start,
            # cannot land the session outside the root that was admitted.
            canonical = await loop.run_in_executor(
                None, dispatch.normalise_root, str(root or ""))
            enrolled = await loop.run_in_executor(
                None, enrollment.enrolled_roots)
            roots = {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
            label = await loop.run_in_executor(
                None, enrollment.enrolled_label, canonical)

            all_cards = await self._board_call("cards") or []
            in_flight = self._launch_inflight(all_cards)
            ok, detail = dispatch.adhoc_guard(
                root=canonical, project=label, roots=roots,
                in_flight=in_flight, tool=str(tool or ""), now=time.time(),
                last_attempt=self._adhoc_attempt or None)
            if not ok:
                return False, detail

            executable = await loop.run_in_executor(
                None, dispatch.resolve_executable, str(tool))
            if not executable:
                return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool=tool)
            argv = dispatch.adhoc_argv(str(tool), executable)
            name = f"{tool} · {label or 'terminal'}"[:40]
            # A terminal Dark Army opened on a press, and it says so too. There is
            # no card here — this is the one kind that names none — but a row
            # nobody can account for is exactly what the stamp exists to
            # prevent, and "I opened it" is as true of this press as of Start.
            spawned, spawn_detail, pid = await dispatch.spawn_local(
                canonical, argv, name, stamp=origin.stamp("adhoc"))
            if not spawned:
                return False, spawn_detail

            now = time.time()
            self._adhoc_attempt = now
            # Resolved here, on the loop, once: straight after `spawn_local`
            # the child's own pid is an exact `_by_pid` hit, so this costs no
            # parent walk — and recording the *handle* is what keeps the
            # sweep in `_launch_inflight` a dict lookup.
            handle = self._pty.owns(pid) or ""
            self._adhoc_launches[secrets.token_hex(4)] = {
                "project": label,
                "root": canonical,
                "handle": handle,
                "at": now,
            }
            logger.info("opened an ad-hoc %s terminal in %s", tool, root)
            return True, spawn_detail

    # ── Mission Control ─────────────────────────────────────────────────

    def _mission_terminal(self):
        """The terminal at the record's handle, or None. Loop-only, like
        every read of `self._pty`."""
        handle = str((self._mission or {}).get("handle") or "")
        if not handle:
            return None
        return self._pty.get(handle)

    def _mission_handle_if_named(self, session_id: str) -> Optional[str]:
        """The recorded Mission Control handle when its last-bound session id
        is `session_id` and the terminal at that handle is alive and not
        named for a *different* session — else None. Resolves only; the
        docstring of `_pty_handle_for` says why it never binds."""
        if not session_id:
            return None
        record = self._mission or {}
        if str(record.get("session_id") or "") != session_id:
            return None
        term = self._mission_terminal()
        if term is None or term.exited:
            return None
        if term.session_id and term.session_id != session_id:
            return None
        return str(record.get("handle") or "") or None

    def _current_brief_digest(self) -> str:
        """`mission.brief_digest` of the brief on disk in Dark Army's own
        checkout, `""` when there is no checkout or the brief is refused.
        Blocking (two file reads); executor only."""
        root = _find_own_checkout()
        if not root:
            return ""
        brief, refusal = mission.read_brief(dispatch.normalise_root(str(root)))
        if refusal:
            return ""
        return mission.brief_digest(brief)

    def _save_mission(self) -> None:
        """Write the record. Called on the executor; the path is read off
        the module at call time so a test can point it elsewhere."""
        mission.save_record(paths.MISSION_PATH, dict(self._mission or {}))

    def _session_origin_by(self, session_id: str) -> str:
        """The `by` of the origin stamp a session carries — off its live
        state, else its tombstone (`_record_finished` copies `origin` onto
        the stub) — or `""` when nothing is known. Loop-only."""
        if not session_id:
            return ""
        state = self._session_states.get(session_id) or {}
        raw = state.get("origin") or ""
        if not raw:
            stub = ((self._finished.get(session_id) or {}).get("stub") or {})
            raw = stub.get("origin") or ""
        return str(origin.parse(str(raw)).get("by") or "")

    def _is_mission_terminal(self, term) -> bool:
        """The identity test both the adopt rung and End apply: the terminal
        wears `mission.PTY_NAME` — a 44-character name no `title[:40]`
        card terminal can carry — and, when a session is bound to it, that
        session does not carry a **known foreign** origin stamp. The stamp
        is fixed in the child's environment by `open_mission` and nowhere
        else, so a session stamped `card-start` (or any kind but `mission`)
        is refused whatever the pty is called. An **absent** stamp is
        accepted, because no stamp anywhere is Mission Control's ordinary
        resting state, not a stranger: quiet past 300s evicts the row, the
        roster re-binds the same id at the next enrich, `_prune_finished`
        drops the tombstone 30 minutes on, and a daemon restart while
        evicted hands the terminal back already bound with neither state
        nor tombstone (`_finished` is not persisted). Refusing it left End
        answering `MISSION_IDENTITY_REFUSAL` while the tab said ready and
        let a lost `mission.json` spawn a second Mission Control. An
        unbound terminal (the seconds before its first hook) passes on the
        name alone, which is what adoption after a restart needs."""
        if term is None or term.exited:
            return False
        if term.name != mission.PTY_NAME:
            return False
        sid = str(term.session_id or "")
        by = self._session_origin_by(sid) if sid else ""
        if sid and by and by != "mission":
            return False
        return True

    def _adopt_named_mission_terminal(self):
        """Belt to the record's braces: a live terminal passing
        `_is_mission_terminal` that the record does not name — a lost or
        corrupt `mission.json` after a restart, the broker still holding the
        process — is adopted into the record rather than doubled. A card's
        terminal, whatever its title, never passes: the pty name is longer
        than a card's `title[:40]` and its session wears a card stamp."""
        for term in self._pty.terminals():
            if self._is_mission_terminal(term):
                return term
        return None

    async def open_mission(self) -> tuple:
        """Start Mission Control, or find it alive. `(ok, detail)` — `detail`
        is the terminal's handle on a fresh spawn (loopback reply-only, and
        only to the panel; never on any snapshot), `MISSION_ALREADY_RUNNING`
        when one is already alive, the refusal in words otherwise.

        `open_adhoc_terminal`'s sibling, and strictly narrower: one
        executable (`claude`), one folder (Dark Army's own checkout, which
        must be enrolled), one fixed brief and one constant opening prompt,
        at most one alive, **idempotent while alive** — the phone opens it on
        every visit to the Comm tab, so a second open while one runs spawns
        nothing. That narrowing is what makes a phone verb defensible here
        where `spawn_terminal` is loopback-only: nothing a caller sends
        reaches the argv, and the worst a stranger with the phone could do
        is start the one session the person would have started anyway.

        Under `_dispatch_lock` for its whole body, so the one-per-project
        launch bound is real across Start, Refine, + TERMINAL and this. The
        launch is recorded in `_adhoc_launches` exactly as an ad-hoc one is,
        which is what holds the bind race for `DISPATCH_BIND_WINDOW`; it
        holds **no board claim** (there is no card) and never counts against
        `board_parallel`. Blocking work goes through the executor.
        """
        if not self.board_dispatch_enabled:
            return False, "Dark Army is not allowed to start sessions (see the ⋯ menu)"
        async with self._dispatch_lock:
            loop = asyncio.get_running_loop()
            if not self._pty.connected:
                return False, MISSION_HOST_RECONNECTING_REFUSAL
            term = self._mission_terminal()
            if term is not None and not term.exited:
                # Alive — but on which brief? The brief rides `--agents`
                # at spawn and nothing re-reads it, so a terminal spawned
                # on an older brief keeps answering from it (a Mission
                # Control spawned "read-only" stayed read-only across the
                # brief that made it act) until it is spawned again. The
                # record's digest is compared with the file's; a match, or
                # a brief that cannot be read right now, keeps what runs.
                # A record with no digest was written by a daemon that
                # compared nothing and is stale by definition.
                # And in which folder? One spawned while Dark Army's own
                # checkout read as a card's side folder stays there after
                # the fix, and Done takes that folder's key back: every
                # board call it makes is then refused as not enrolled.
                recorded = str((self._mission or {}).get("brief_digest") or "")
                current = await loop.run_in_executor(
                    None, self._current_brief_digest)
                stale_brief = bool(current) and recorded != current
                stale_folder = await loop.run_in_executor(
                    None, _mission_folder_is_stale, term.root)
                if not stale_brief and not stale_folder:
                    return True, MISSION_ALREADY_RUNNING
                why = "an older brief" if stale_brief else "another folder"
                closed = await self._pty.close(term.handle)
                if not closed:
                    logger.warning("Mission Control is on %s but its close "
                                   "was unconfirmed (%s); kept", why,
                                   term.handle)
                    return True, MISSION_ALREADY_RUNNING
                logger.info("closed Mission Control running on %s (%s); "
                            "spawning again", why, term.handle)
            adopted = self._adopt_named_mission_terminal()
            if adopted is not None and await loop.run_in_executor(
                    None, _mission_folder_is_stale, adopted.root):
                # Adopted by name after a lost record — but in the wrong
                # folder, the same case as above: replace, never adopt.
                if not await self._pty.close(adopted.handle):
                    logger.warning("Mission Control is in another folder "
                                   "but its close was unconfirmed (%s); "
                                   "kept", adopted.handle)
                    return True, MISSION_ALREADY_RUNNING
                logger.info("closed Mission Control running in another "
                            "folder (%s); spawning again", adopted.handle)
                adopted = None
            if adopted is not None:
                # A lost record says nothing about the brief; the adopted
                # terminal is taken as current and compared from here on.
                self._mission = {
                    **dict(self._mission or {}),
                    "handle": adopted.handle,
                    "root": adopted.root,
                    "session_id": adopted.session_id or "",
                    "opened_at": float(adopted.started_at or time.time()),
                    "ended": False,
                    "ended_at": 0.0,
                    "brief_digest": await loop.run_in_executor(
                        None, self._current_brief_digest),
                }
                await loop.run_in_executor(None, self._save_mission)
                logger.info("adopted a live Mission Control terminal (%s)",
                            adopted.handle)
                return True, MISSION_ALREADY_RUNNING

            root = await loop.run_in_executor(None, _find_own_checkout)
            if not root:
                return False, MISSION_NO_CHECKOUT_REFUSAL
            canonical = await loop.run_in_executor(
                None, dispatch.normalise_root, str(root))
            enrolled = await loop.run_in_executor(
                None, enrollment.enrolled_roots)
            roots = {dispatch.normalise_root(r) for r in enrolled if r}
            roots.discard("")
            if canonical not in roots:
                return False, MISSION_NOT_ENROLLED_REFUSAL
            label = await loop.run_in_executor(
                None, enrollment.enrolled_label, canonical)

            brief, refusal = await loop.run_in_executor(
                None, mission.read_brief, canonical)
            if refusal:
                return False, refusal

            all_cards = await self._board_call("cards") or []
            in_flight = self._launch_inflight(all_cards)
            ok, detail = dispatch.adhoc_guard(
                root=canonical, project=label, roots=roots,
                in_flight=in_flight, tool="claude", now=time.time(),
                last_attempt=self._mission_attempt or None)
            if not ok:
                return False, detail

            executable = await loop.run_in_executor(
                None, dispatch.resolve_executable, "claude")
            if not executable:
                return False, dispatch.NOT_INSTALLED_REFUSAL.format(tool="claude")
            argv = dispatch.mission_argv(
                executable,
                card_prepare.agents_json(
                    mission.AGENT_NAME, mission.AGENT_DESCRIPTION, brief),
                mission.allowed_tools(canonical), mission.AGENT_NAME,
                mission.OPENING_PROMPT, display_name=mission.NAME)
            spawned, spawn_detail, pid = await dispatch.spawn_local(
                canonical, argv, mission.PTY_NAME,
                stamp=origin.stamp("mission"))
            if not spawned:
                return False, spawn_detail

            now = time.time()
            self._mission_attempt = now
            handle = self._pty.owns(pid) or ""
            # A prior record whose terminal has exited is replaced whole:
            # the exited terminal is left for `PtyHost.reap`.
            self._mission = {
                "handle": handle,
                "root": canonical,
                "session_id": "",
                "opened_at": now,
                "ended": False,
                "ended_at": 0.0,
                "brief_digest": mission.brief_digest(brief),
            }
            await loop.run_in_executor(None, self._save_mission)
            self._adhoc_launches[secrets.token_hex(4)] = {
                "project": label,
                "root": canonical,
                "handle": handle,
                "at": now,
            }
            logger.info("opened Mission Control in %s", canonical)
            return True, handle

    async def end_mission(self) -> tuple:
        """Close Mission Control's terminal. `(ok, detail)`.

        The one thing that ends it: leaving the tab, closing the cover,
        closing the app and restarting Dark Army all leave it running. The
        identity is re-checked at the moment it fires, `stop_session`'s
        shape: the terminal at the recorded handle must still pass
        `_is_mission_terminal` — the pty name `mission.PTY_NAME` and, when a
        session is bound, no known foreign origin stamp — and a handle is an
        opaque token the host mints once and never reuses, so a stale record
        can only miss, never aim at another terminal. The row's eviction is
        **not** done here: the hosted terminal going is what
        `_retire_hostless_sessions` / `_check_liveness` see, and they route
        through `_forget_session(sid, TERMINAL_CLOSED_REASON)` as for every
        hosted end. No `PROMPT_BLOCKED_REFUSAL`: End is the person choosing
        to stop.

        **The record is blanked only on a confirmed close.** `PtyHost.close`
        answers False when the broker did not say ok (a timeout, a lost
        link); the process may still be alive, and a record blanked on a
        guess would answer "not running" to the next End and let the next
        open spawn a second one. On False the record stays and the reply
        says so. On True the record keeps `ended` / `ended_at` — the broker
        forgets a closed terminal, so this is the only way the snapshot can
        say `ended` rather than `off` — cleared by the next open.
        """
        async with self._dispatch_lock:
            loop = asyncio.get_running_loop()
            record = dict(self._mission or {})
            handle = str(record.get("handle") or "")
            term = self._mission_terminal()
            if not handle or term is None:
                return False, MISSION_NOT_RUNNING_REFUSAL
            if term.handle != handle or not self._is_mission_terminal(term):
                return False, MISSION_IDENTITY_REFUSAL
            closed = await self._pty.close(handle)
            if not closed:
                logger.warning("Mission Control close unconfirmed (%s); "
                               "record kept", handle)
                return False, MISSION_CLOSE_FAILED_REFUSAL
            record["handle"] = ""
            record["session_id"] = ""
            record["ended"] = True
            record["ended_at"] = time.time()
            self._mission = record
            await loop.run_in_executor(None, self._save_mission)
            logger.info("ended Mission Control (%s)", handle)
            return True, "ended"

    def mission_snapshot(self) -> dict:
        """The `mission` section of `/api/state`: whether Mission Control is
        running, which session it is and where it lives. **Never a handle,
        key, digest, claim or token** — the handle is loopback reply-only.

        `alive` is `connected` and a terminal at the record's handle that
        has not exited; `session_id` is the terminal's own name when it has
        one, else the record's last-bound id (a quiet, evicted Mission
        Control keeps its id on the wire until the roster re-binds it).
        When the terminal wears an id the record does not know — a `/clear`
        successor — the record is updated in memory and its write is
        **scheduled** on the executor, never awaited inside `state()`.
        """
        record = self._mission or {}
        term = self._mission_terminal()
        connected = bool(self._pty.connected)
        alive = bool(connected and term is not None and not term.exited)
        # `exited` is the terminal's word when there is one, else the
        # record's `ended` mark: a confirmed End removes the terminal from
        # the host, so without the record the tab could only say `off`.
        exited = bool(term is not None and term.exited) or bool(
            term is None and record.get("ended"))
        session_id = str(record.get("session_id") or "")
        current = ""
        if term is not None and term.session_id:
            current = term.session_id
            # The broker keeps a terminal's name across a Dark Army restart
            # and hands it back on re-adopt, so a terminal can still wear an
            # id this daemon has already forgotten (evicted, then `/clear`ed).
            # When a stamped successor is running in it, the stale name is
            # dropped — `_forget_session`'s own `unbind`, which it missed —
            # and the next enrich binds the successor the ordinary way.
            if alive and current not in self._session_states:
                successor = self._mission_successor(term)
                if successor and successor != current:
                    self._pty.unbind(current)
                    current = successor
        elif alive:
            current = self._mission_successor(term)
        if current and current != session_id:
            self._mission = {**dict(record), "session_id": current}
            self._schedule_mission_save()
        if current:
            session_id = current
        return {
            "available": True,
            "alive": alive,
            "exited": exited,
            "session_id": session_id,
            "root": str(record.get("root") or ""),
            "name": mission.NAME,
            "opened_at": float(record.get("opened_at") or 0.0),
        }

    def _mission_successor(self, term) -> str:
        """The live session running inside Mission Control's terminal when
        the terminal wears no name, else `""`.

        The case this answers: a quiet Mission Control is evicted, which
        unbinds its terminal's name, and then a `/clear` starts a fresh id
        in the same process. Nothing re-binds that id, so the record kept
        the evicted one, and every check keyed on it — `ask_start`'s
        "only Mission Control", Bearings' skip, the Comm tab — answered for
        a session that no longer exists. Resolved, never bound (the
        `_mission_handle_if_named` rule): a candidate must carry the
        `mission` origin stamp *and* run in this terminal by pid, so a card
        session or a helper Mission Control spawned (no such stamp) never
        passes. The newest by last event wins. Loop-only."""
        handle = str(getattr(term, "handle", "") or "")
        if not handle:
            return ""
        best, best_at = "", -1.0
        for sid, st in self._session_states.items():
            if origin.parse(str(st.get("origin") or "")).get("by") != "mission":
                continue
            pid = st.get("pid")
            if not pid or self._pty.owns(pid) != handle:
                continue
            at = float(st.get("last_event_monotonic") or 0.0)
            if at > best_at:
                best, best_at = sid, at
        return best

    def _schedule_mission_save(self) -> None:
        """Book `_save_mission` on the executor from the loop; written
        synchronously where no loop is running (a test calling `state()`
        directly)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_mission()
            return
        loop.run_in_executor(None, self._save_mission)

    async def prepare_card_text(self, fields: dict) -> tuple:
        """Ask the card's assistant, on its cheap model, to write a new card's
        instructions (`card_prepare.HELPER_MODELS`). `(result|None, detail)`.

        Cheap field checks first, so a person fixes the form before a model
        call is spent; then the single-in-flight bound. The helper's cwd is
        the card's project
        root — the named departure from `_make_title`, which runs in
        `STATE_DIR` so a namer cannot load somebody's CLAUDE.md. Prepare
        should see that project's conventions, bounded by the same known-roots
        membership `dispatch.guard` already uses.
        """
        fields = dict(fields or {})
        # One box or four. A newer client with a blank description puts the
        # idea in `summary` too, so an older daemon still works; this one
        # prefers `idea` and ignores the duplicate, or the same words would
        # be clamped twice under two names.
        idea = str(fields.get("idea") or "").strip()
        summary = str(fields.get("summary") or "").strip()
        if idea:
            if len(idea) > card_prepare.MAX_IDEA_INPUT:
                return None, (f"the idea is longer than "
                              f"{card_prepare.MAX_IDEA_INPUT} characters")
        else:
            if not summary:
                return None, "a card needs a description"
            if len(summary) > board.MAX_SUMMARY_CHARS:
                return None, (f"the summary is longer than "
                              f"{board.MAX_SUMMARY_CHARS} characters")
        root = dispatch.normalise_root(str(fields.get("root") or ""))
        if not root:
            return None, "this card does not say which folder to work in"
        loop = asyncio.get_running_loop()
        known = await loop.run_in_executor(None, self._known_project_roots)
        known_roots = {dispatch.normalise_root(r) for r in known}
        if root not in known_roots:
            return None, "no open window for that project"
        # The closed set the helper may name a folder from. It is the
        # *intersection*: `_known_project_roots` also holds live-session cwds
        # with no open window, and a suggestion naming one of those would be
        # unselectable in either composer — the picker lists the board
        # snapshot's `projects` and nothing else. An in-memory attribute read
        # on the loop, deliberately not a second executor hop.
        offered: list = []
        seen_offer = set()
        for project in (self._board_state or {}).get("projects") or []:
            candidate = dispatch.normalise_root(str(project.get("root") or ""))
            if not candidate or candidate in seen_offer:
                continue
            if candidate not in known_roots:
                continue
            seen_offer.add(candidate)
            offered.append(candidate)
        if not os.path.isdir(root):
            return None, "that project folder is not there any more"
        rels = attachments.split_field(fields.get("attachments"))
        if rels:
            reason = attachments.field_refusal(
                str(fields.get("attachments") or ""))
            if reason:
                return None, reason
            abs_paths, reason = await loop.run_in_executor(
                None, attachments.read_refusal, rels)
            if reason:
                return None, reason
            fields["attachment_paths"] = abs_paths or []
        else:
            fields["attachment_paths"] = []

        # Refuse rather than queue. The lock is held across the helper so the
        # bound is enforced, not computed; `_preparing` and `locked()` are
        # checked first so a second press returns immediately rather than
        # waiting up to TIMEOUT_SECONDS.
        if self._preparing or self._prepare_lock.locked():
            return None, PREPARE_BUSY_DETAIL
        async with self._prepare_lock:
            if self._preparing:
                return None, PREPARE_BUSY_DETAIL
            now = time.monotonic()
            if now - self._last_prepare_at < self.PREPARE_COOLDOWN:
                return None, PREPARE_BUSY_DETAIL
            self._preparing = True
            try:
                return await self._prepare_card_text_locked(
                    fields, root, roots=offered)
            finally:
                self._preparing = False
                self._last_prepare_at = time.monotonic()

    async def _prepare_card_text_locked(self, fields: dict, root: str,
                                        roots=()) -> tuple:
        """The body of `prepare_card_text`, under `_prepare_lock`. No other caller.

        `roots` is the closed set of folders the answer may name, keyword with
        an empty default so a caller that has no opinion asks for none — with
        fewer than two the prompt is byte-identical to today's.
        """
        # The card's own assistant writes its card, on that assistant's
        # cheap model; a name Dark Army cannot run headless falls back to claude.
        helper = card_prepare.helper_tool(fields.get("tool"))
        attached = tuple(fields.get("attachment_paths") or ())
        if attached and helper != "claude":
            return None, ("this assistant cannot read attached files — "
                          "pick Claude, or drop the photos")
        executable = await asyncio.get_running_loop().run_in_executor(
            None, dispatch.resolve_executable, helper)
        if not executable:
            return None, dispatch.NOT_INSTALLED_REFUSAL.format(tool=helper)
        # The project's real subagent roster, read fresh on each press. A
        # directory walk, so it runs on the executor — reading files on the
        # loop stalls SSE broadcasts for every panel. It never raises: a
        # broken agents directory is an empty roster, not a refusal.
        roster = await asyncio.get_running_loop().run_in_executor(
            None, card_prepare.read_roster, root)
        loop = asyncio.get_running_loop()
        brief = await loop.run_in_executor(None, card_prepare.read_brief, root)
        brief_path = ""
        if helper == "grok":
            brief_path = str(
                paths.ensure_state_dir() / card_prepare.GROK_AGENT_FILENAME)
            payload = card_prepare.grok_agent_file(
                brief.name, brief.description, brief.body)

            def _write_grok_agent(path=brief_path, text=payload):
                Path(path).write_text(text, encoding="utf-8")

            try:
                await loop.run_in_executor(None, _write_grok_agent)
            except OSError:
                return None, (
                    "Dark Army could not write the preparer agent for grok")
        timeout = (card_prepare.TIMEOUT_WITH_ATTACHMENTS_SECONDS
                   if attached else card_prepare.TIMEOUT_SECONDS)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *card_prepare.argv(
                    executable,
                    # The card-preparer slot for the helper that runs it;
                    # `""` keeps `HELPER_MODELS`' cheap default.
                    model=self._agent_model_for(root, helper, "card-preparer"),
                    title=str(fields.get("title") or ""),
                    summary=str(fields.get("summary") or ""),
                    tool=str(fields.get("tool") or ""),
                    project=str(fields.get("project") or ""),
                    roster=roster,
                    roots=roots,
                    attachments=attached,
                    idea=str(fields.get("idea") or ""),
                    brief=brief.body,
                    brief_name=brief.name,
                    brief_description=brief.description,
                    brief_path=brief_path,
                ),
                # Closed rather than inherited: `codex exec` appends a piped
                # stdin to the prompt and waits for its EOF.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # The card's project, not STATE_DIR: Prepare should see that
                # project's CLAUDE.md. `_make_title` refuses this on purpose.
                cwd=root,
                # None for claude (inherit); the hook port pointed at nothing
                # for the two CLIs that cannot switch Dark Army's hooks off.
                env=card_prepare.env_for(helper),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return None, "Dark Army took too long to write the card text"
        except (OSError, ValueError):
            return None, f"could not start {helper}"
        if proc.returncode:
            return None, f"{helper} did not write the card text"
        raw = out.decode("utf-8", "replace")
        idea = str(fields.get("idea") or "").strip()
        title = summary = ""
        objective: dict = {}
        if idea:
            title, summary, prompt, stages = card_prepare.parse_idea(
                raw, roster=roster)
            # A bad title or description refuses the whole call. Returning
            # just the instructions would fill two boxes of four and read as
            # success — the person would have to notice what was *not*
            # written before they could put it right.
            reason = (card_prepare.title_refusal(title)
                      or card_prepare.summary_refusal(summary))
            if reason:
                self._log_prepare_refusal(reason, raw)
                return None, reason
            # The objective is a suggestion: an absent answer is an empty
            # box, never a refusal. An over-long one refuses the whole press
            # for `title_refusal`'s reason — reject rather than trim words
            # the person would then have to notice were cut.
            objective = card_prepare.parse_objective(raw)
            reason = card_prepare.objective_refusal(objective)
            if reason:
                self._log_prepare_refusal(reason, raw)
                return None, reason
        else:
            prompt, stages = card_prepare.parse(raw, roster=roster)
        # A stored prompt never carries attachment links: the block is
        # dispatch's job, added once at the moment an assistant is started.
        # The helper is told not to list the paths; this strips any it wrote
        # anyway, *before* `refusal` judges the text — so an answer that was
        # nothing but paths refuses as unusable instead of storing blank, and
        # the length bound judges the form that is actually saved.
        if attached:
            prompt = attachments.strip_path_lines(prompt, attached)
        reason = card_prepare.refusal(prompt, str(fields.get("tool") or ""))
        if reason:
            self._log_prepare_refusal(reason, raw)
            return None, reason
        # Computed *after* the refusals, so an unusable or off-list folder is
        # no suggestion rather than a refusal of the whole call — and a bad
        # prompt still refuses for the reason it refuses today.
        # The area falls back to Universal in both modes: a decorated answer
        # (`**Pocket** — phones`) normalises to its own slug first inside
        # `parse_area`, so the fallback is taken only where the helper named
        # nothing usable (`NONE`, off-list, no section). Both composers apply
        # the suggestion only into an empty Area box, so a chosen area is
        # never overwritten.
        result = {"prompt": prompt, "workflow": board.join_stages(stages),
                  "suggested_root": card_prepare.parse_folder(raw, roots),
                  "suggested_area":
                      card_prepare.parse_area(raw) or areas.UNIVERSAL}
        if idea:
            result["title"] = title
            result["summary"] = summary
            # Idea mode only — the legacy press's reply keys are unchanged.
            # Possibly empty: both composers apply a suggestion only into an
            # empty box, and empty means "nothing offered".
            result.update(objective)
        return result, ""

    def _log_prepare_refusal(self, reason: str, raw: str) -> None:
        """One log line per parse refusal, with a bounded excerpt of what
        the helper actually wrote, so the next intermittent formatting
        failure can be diagnosed rather than guessed at. Called at the three
        parse-refusal returns only — never on success (a line per press
        would fill the daily log with card drafts), the timeout, a non-zero
        exit or any pre-spawn refusal."""
        logger.info("Prepare refused (%s) — helper answer: %s",
                    reason, card_prepare.log_excerpt(raw))

    def _live_session_ids(self) -> set:
        """Every session id the fleet currently has, from every register that
        holds one. Union rather than the snapshot alone: the snapshot is up to a
        few seconds old, and a baseline that missed a session would let a card
        bind to one that was already running."""
        ids = set(self._session_states)
        ids |= set(self._agent_records)
        ids |= set(self._grok_records)
        ids |= set(getattr(self, "_codex_records", {}) or {})
        for group in (self._agents_snapshot_cache or {}).values():
            if isinstance(group, list):
                for row in group:
                    if isinstance(row, dict) and row.get("session_id"):
                        ids.add(row["session_id"])
        return ids

    def _claiming_session_ids(self) -> set:
        """Sessions whose card still holds its project's files.

        Narrower than `_live_session_ids`, and deliberately so: that one asks
        *does this session exist* (a baseline, where missing one would let a
        card bind to a session that was already running), and this one asks
        *is somebody editing this tree right now*, where a wrong "yes" stalls
        every other card in the project until a person closes a terminal.

        The hook stream is the evidence. A session in `_session_states` has
        emitted an event inside the staleness window; one that has not is
        evicted from it, and — this is the wedge that produced this method —
        comes back as a roster-only stub in the `running` bucket for as long
        as its terminal stays open, because `claude agents --json` goes on
        listing it as busy. Reading a row instead of a hook stream therefore
        meant a card stayed `live` for hours after its work was finished.

        **Only pipeline main agents may hold a project** — a board card's own
        dispatched top-level session. The hook stream is the core of the set;
        a stray Grok child firing hooks under its own id is skipped
        (`_parent_of_child` — a subagent is never a holder in its own right,
        while its parent's entry, kept alive by child-event redirection and
        the subagent eviction exemption, keeps the claim exactly as before).
        The Grok and Codex registers stay because their harnesses emit no hook
        stream to be evicted from — Grok's roster whole (root TUIs only by
        construction), Codex only its roots, since a child record is
        background work by definition.

        The background-agent loop over `_agent_records` is deliberately
        **gone**, reversing a documented over-claiming choice: a background
        agent may never hold a project, and now that `_bindable_candidate`
        keeps one from ever being bound to a card, its ids here only ever
        served wrongly-bound cards — the wedge where four queued cards waited
        on a background job. The unnamed-kind branch went with it, so a quiet
        pipeline main whose roster row carries `kind: ""` now releases after
        hook eviction — consistent with the rule that a session quiet for
        five minutes is not editing anything, and stated here rather than
        changed in silence.
        """
        # Over a `list(...)` snapshot: this runs on the executor (the
        # reconcile, `_build_board_state`, the queue gate) while the loop
        # mutates `_session_states` — and the predicate re-enters the same
        # dict, so a live iterator here raised "dictionary changed size
        # during iteration" whenever a session started mid-frame.
        ids = {sid for sid in list(self._session_states)
               if not self._parent_of_child(sid)}
        ids |= set(self._grok_records)
        ids |= {sid for sid, rec in
                (getattr(self, "_codex_records", {}) or {}).items()
                if not getattr(rec, "is_child", bool(getattr(rec, "parent_thread_id", "")))}
        return ids

    def _mid_turn_quiet_ids(self) -> set:
        """Sessions Dark Army lost sight of *during* a turn, and only for silence.

        `_claiming_session_ids()`' companion, and the reason it exists is
        that the two answer different questions. That one asks "is somebody
        editing this tree" and reads hook-stream freshness, where a wrong
        "no" costs a slot; this one exists so `board_queue.needs_you` can ask
        "did this card lose its assistant", where a wrong "yes" puts a card
        somebody is working under a person's Needs you with *Mark done* and
        *Send back* beside it.

        The evidence is the tombstone `_forget_session` leaves. Two terms,
        both load-bearing. `end_reason == "evicted"` is wall-clock staleness
        alone — the removal the daemon already documents as a statement about
        *our bookkeeping* rather than about the tab (`GROK_END_REASONS`
        excludes it for that very reason); a dead process retires as `no
        process` and is a real end, so it is deliberately not here. And the
        frozen state is `MID_TURN_STATES`: a session that had finished its
        turn was last seen `idle` and is left out, so the case the predicate
        was built for — work over, terminal still open, nobody has said what
        happens next — still reaches the person.

        The window is the tombstone's own (`FINISHED_RETENTION_SECONDS`, 30
        minutes), inherited rather than invented: past it the silence is long
        enough that asking a person is the better answer.
        """
        out = set()
        for sid, record in list((getattr(self, "_finished", None) or {}).items()):
            if not isinstance(record, dict):
                continue
            if str(record.get("end_reason") or "") != "evicted":
                continue
            stub = record.get("stub") or {}
            if str(stub.get("state") or "") in board_queue.MID_TURN_STATES:
                out.add(sid)
        return out

    def _reconcile_board(self, snapshot: dict) -> bool:
        """Bring every card's link to its session up to date. Blocking; runs on
        the executor inside `_push_agents_snapshot`. Returns whether anything
        changed, so a quiet board buys no frame.

        Three things, and none of them is a status. A card's session is in a live
        bucket, so the link is `live`; it is not, and has not been for
        `BOARD_SESSION_GRACE`, so the link is `ended`; or the card is still
        `dispatching` and gets one more chance to bind.

        **A card is never moved to Done here.** A session ending says nothing
        about whether the work was finished, and Dark Army deciding otherwise is the
        one thing a board must not do. `ended` is a fact about the *card* — the
        session's own category is read live off the agents snapshot by whoever
        draws it, so `running`/`waiting`/`sleeping` are never written into
        board.db and the two can never disagree.

        The card outlives the tombstone by construction: `_forget_session` pops
        the session and `_finished` expires it half an hour later, and neither
        path touches another database.
        """
        if self._board is None:
            return False
        live = self._board_live_ids(snapshot)
        now = time.time()
        changed = False
        try:
            cards = self._board.cards()
        except Exception:
            logger.debug("board reconcile could not read cards", exc_info=True)
            return False

        # A card's project label follows the enrolled folder's name, once.
        # The board joins card to session **by label** — `_bind_dispatched_card`
        # refuses a candidate whose row `project` differs before it ever looks
        # at the cwd, and `board_queue.claims` / `_slot_refusal` group the slot
        # rule the same way — so a card written before its project was renamed
        # could never be matched to the session it starts (it would time out
        # after `DISPATCH_BIND_WINDOW` with an orange line, for ever) and its
        # project's queue would count in two halves. Steady state is one string
        # compare per distinct root per pass; a failed relabel costs the
        # relabel, never the reconcile.
        try:
            wanted: dict = {}
            for card in cards:
                croot = str(card.get("root") or "")
                if not croot or croot in wanted:
                    continue
                lbl = enrollment.enrolled_label(croot)
                if lbl:
                    wanted[croot] = lbl
            relabelled = False
            for card in cards:
                croot = str(card.get("root") or "")
                lbl = wanted.get(croot, "")
                if lbl and lbl != str(card.get("project") or ""):
                    if self._board.relabel_root(croot, lbl):
                        relabelled = True
                    wanted.pop(croot, None)
            if relabelled:
                # Re-read before anything below reads a card's project: the
                # bind comparison in this same pass would otherwise miss on the
                # stale in-memory dict and wait a snapshot.
                cards = self._board.cards()
                changed = True
        except Exception:
            logger.debug("board reconcile could not follow an enrolled rename",
                         exc_info=True)

        seen = set()
        for card in cards:
            cid = card.get("id") or ""
            seen.add(cid)
            # Queue the card for its importance number before the link
            # blocks below — they `continue` freely, and a card must not miss
            # its one look at the scorer because it happened to be bound to
            # a session.
            try:
                self._consider_priority(card)
            except Exception:
                logger.debug("priority consideration failed for %s", cid,
                             exc_info=True)
            # A Done card still naming a folder whose link is not live — after
            # a restart, or with its folder removed by hand — is looked at
            # once per process (`docs/card-worktrees.md`, *Release at Done*).
            try:
                self._consider_standing_worktree(card)
                self._consider_standing_branch(card)
            except Exception:
                logger.debug("worktree consideration failed for %s", cid,
                             exc_info=True)
            # The refinement's own reconcile, before the dispatch's and never
            # instead of it: a refining card has no session link (its guard
            # refused one), so the two blocks cannot fight over one card, and
            # a card whose refinement `ended` can later be dispatched and is
            # then tracked by the block below like any other.
            rstate = card.get("refine_state") or ""
            if rstate == "dispatching":
                changed |= self._bind_refining_card(card, snapshot, now)
            elif rstate == "live":
                rsid = card.get("refine_session_id") or ""
                if rsid and rsid in live:
                    self._refine_missing_since.pop(cid, None)
                    # The "who planned it" half. Same verb, same writer, same
                    # guard as the dispatch branch below — a refinement's
                    # `bc-planner` was recorded nowhere before this line.
                    changed |= self._record_card_stages(cid, rsid)
                else:
                    first = self._refine_missing_since.setdefault(cid, now)
                    if now - first >= self.BOARD_SESSION_GRACE:
                        # The id is kept as a record; `ended` is what makes
                        # Refine appear again (`refine_guard` admits it). No
                        # column move — a session ending says nothing about
                        # whether a plan was written, and if one *was*,
                        # `attach_plan` already moved the card.
                        if card.get("batch_id"):
                            # A batch member still in Prep when its one
                            # session went is a card the batch never
                            # reached: say so, or it reads as refined.
                            self._board.update(cid, {
                                "refine_state": "ended",
                                "batch_id": "",
                                "batch_rank": "",
                                "dispatch_error": BATCH_UNPLANNED_NOTE.format(
                                    rank=str(card.get("batch_rank") or "?")),
                            })
                        else:
                            self._board.update(cid, {"refine_state": "ended"})
                        self._refine_missing_since.pop(cid, None)
                        changed = True
            state = card.get("link_state") or ""
            if state == "dispatching":
                changed |= self._bind_dispatched_card(card, snapshot, now)
                continue
            sid = card.get("session_id") or ""
            if not sid:
                continue
            if sid in live:
                self._board_missing_since.pop(cid, None)
                if state == "ended" and card.get("batch_id"):
                    # A batch member its session moved past
                    # (`advance_batch_by_session` wrote `ended` while the
                    # session lives on, on the next card). It stays ended:
                    # flipping it back to `live` would make the session look
                    # bound to two open cards, and its stages are the next
                    # card's now.
                    continue
                if state != "live":
                    self._board.mark_live(cid)
                    changed = True
                changed |= self._record_card_stages(cid, sid)
                continue
            first_missing = self._board_missing_since.setdefault(cid, now)
            if state != "ended" and now - first_missing >= self.BOARD_SESSION_GRACE:
                self._board.mark_ended(cid, when=now)
                bid = str(card.get("batch_id") or "")
                if bid:
                    # The batch's one session has gone: every member it
                    # never reached goes back to plain Backlog with a note,
                    # in one statement, and the card it was on leaves the
                    # batch with it — so a resume of that session finds an
                    # ordinary interrupted card, not a member to keep ended.
                    # Only the batch's own session releases it: a card that
                    # left Backlog with its mark and ran alone is not.
                    if self._batch_owned_by(card):
                        self._board.release_batch_waiting(bid, BATCH_LEFT_NOTE)
                    if str(card.get("column_name") or "") != "done":
                        self._board.update(cid, {"batch_id": "",
                                                 "batch_rank": ""},
                                           bump=False)
                # The one seam every ended run passes through, whichever way
                # it ended — the assistant closed the card, left a hand-check,
                # or simply went quiet. `_record_finished` is the *session*
                # seam and is the wrong one here: it has no card in hand.
                self._consider_work_record(card, snapshot)
                # And the run's final health reading, off the same row, so
                # the card keeps its line once the tombstone has gone.
                self._freeze_run_health(card, snapshot)
                # And a Done card's own worktree, now that nothing runs in
                # it: queued here, decided again and removed on the loop
                # (`_flush_worktree_releases`), after its record is read.
                self._consider_worktree_release(card)
                changed = True
        # `list(...)` first: this runs on the executor while the dispatch path
        # pops both dicts on the loop, and a live iteration would raise
        # "dictionary changed size during iteration".
        for cid in [c for c in list(self._board_missing_since) if c not in seen]:
            self._board_missing_since.pop(cid, None)
        for cid in [c for c in list(self._refine_missing_since) if c not in seen]:
            self._refine_missing_since.pop(cid, None)
        # The queue's decision, last: it reads the link states this pass has
        # just written, so a session that left the live buckets frees its
        # project's files in the same reconcile that noticed. The list is
        # acted on by `_flush_queue_dispatches` on the loop — nothing here
        # starts anything. One `_claiming_session_ids()` reading for the pass
        # — the decision and the drift check re-computing it could disagree
        # inside one reconcile, and the walk is not free.
        active = self._claiming_session_ids()
        self._decide_queue_dispatches(cards, active=active, snapshot=snapshot)
        changed |= self._bind_consults(snapshot, now)
        changed |= self._manual_due_drifted(cards, snapshot, active=active)
        changed |= self._needs_you_drifted(cards, active=active)
        self._observe_board_outcomes(snapshot, active)
        # After the observers, so the ledgers this reads have just been
        # written: the cent, the minute and the percent that moved on this
        # pass buy the frame that draws them.
        changed |= self._run_figures_drifted()
        # And the lead each waiting card promises: a character coming or
        # going from the screen can move the free pool member it names.
        changed |= self._lead_faces_drifted()
        # And the run-health line, off the same snapshot this pass read:
        # a quantum of turns, tokens or context that moved buys the frame
        # that draws it.
        changed |= self._run_health_drifted(cards, snapshot)
        # Last, and queue-only: the enrolled projects whose dead git worktree
        # registrations have not been looked at in this process.
        try:
            self._consider_worktree_prunes()
        except Exception:
            logger.warning("queueing the worktree prunes failed",
                           exc_info=True)
        return changed

    def _reconcile_review(self) -> bool:
        """The review runs' own executor step, beside `_reconcile_board` and
        independent of it: the findings file landing, a step line, a
        terminal ending. It runs with no board open and after a raise in
        the board's pass. The pty facts were composed on the loop before
        the hop; nothing here touches the host."""
        try:
            changed = self._observe_review_runs(self._review_pty_facts)
            changed |= self._review_drifted()
            return changed
        except Exception:
            logger.warning("review reconcile failed", exc_info=True)
            return False

    def _run_health_drifted(self, cards: list, snapshot: dict) -> bool:
        """Buy a board frame when a card's run-health line moved.

        Executor only, at the end of `_reconcile_board`. The reading is
        derived from the agents snapshot and never stored, so without this
        a live run's line would sit at whatever the last board *write*
        published. Composes the same dict `_run_health_for` publishes —
        off the snapshot this pass was handed, which `_push_agents_snapshot`
        has already made the cache — for every card with a session id, and
        compares it with the last pass: `_manual_due_drifted`'s shape, and
        the quantisation inside `run_health.compose` (turns exact to 20
        then by five, tokens to two figures, context by five) is what bounds
        this to one frame per quantum rather than one per turn.

        Compared per card **present on this pass**: a card whose row has
        left the snapshot keeps its last key rather than buying a frame,
        because the one seam that ends a run (`mark_ended`, above) buys that
        frame itself and freezes the line the card keeps. A card gone from
        the board is dropped from the memo. A failure logs at debug and
        buys nothing.
        """
        try:
            if self._board is None:
                return False
            bound = [c for c in cards
                     if str(c.get("id") or "") and str(c.get("session_id") or "")]
            counts = self._board.run_health_counts(
                [c["id"] for c in bound]) if bound else {}
            ledger = run_health.Ledger.load()
            seen = {}
            for card in bound:
                reading = self._run_health_for(card, counts, ledger,
                                               snapshot=snapshot)
                if reading is not None:
                    seen[str(card["id"])] = reading
            on_board = {str(c.get("id") or "") for c in cards}
            last = self._run_health_seen
            changed = any(last.get(cid) != reading
                          for cid, reading in seen.items())
            kept = {cid: r for cid, r in last.items() if cid in on_board}
            kept.update(seen)
            self._run_health_seen = kept
            return changed
        except Exception:
            logger.debug("run-health drift check failed", exc_info=True)
            return False

    def _busy_lead_stems(self) -> frozenset:
        """Every character on screen now, by stem, plus the art-only faces —
        `_role_nickname`'s busy set, read the same way."""
        shown = getattr(self, "_nicknames_shown", {}) or {}
        busy = {str(n).split("-")[0].lower() for n in shown.values() if n}
        busy |= {s.lower() for s in identity.ART_ONLY}
        return frozenset(busy)

    def _promised_lead(self, card: dict) -> str:
        """The lead a card shows before Start, and the one Start then gives.

        The card used to draw its area's *usual* lead whatever the fleet was
        doing, while Start took the first *free* pool member — so pressing
        Start on a Gate card while Nyx was busy swapped Nyx's face for
        Watch's. The promise is computed here from who is on screen and held
        per card; while the card's link is `dispatching` or `live` it is
        frozen, so the new session (named from the promise by
        `_role_nickname`) never pushes the card onto the next free member.
        Executor-only, like both of its readers. `""` with no pool or with
        every member busy (the identity store then picks, as before).
        """
        cid = str(card.get("id") or "")
        promises = self.__dict__.setdefault("_lead_promises", {})
        if not cid or str(card.get("column_name") or "") == "done":
            promises.pop(cid, None)
            return ""
        link = str(card.get("link_state") or "")
        if link in ("dispatching", "live"):
            # A bound session on screen is the truth: after a restart the
            # promise is gone and the card's own session counts as busy, so
            # recomputing would draw the next pool member instead.
            sid = str(card.get("session_id") or "")
            shown = getattr(self, "_nicknames_shown", {}) or {}
            if sid and shown.get(sid):
                promises[cid] = str(shown[sid]).split("-")[0].lower()
                return promises[cid]
            if cid in promises:
                return promises[cid]
        slug = str(card.get("area") or "")
        busy = self._busy_lead_stems()
        who = areas.allocate(slug, cid, busy)
        if not who or who in busy:
            who = ""
        promises[cid] = who
        return who

    def _lead_faces_drifted(self) -> bool:
        """Buy a board frame when the set of faces on screen moved, since
        that can move the lead a waiting card promises. The first pass
        compares against an empty screen, as `_run_figures_seen` starts
        empty: nobody on screen yet is not a move."""
        busy = self._busy_lead_stems()
        empty = frozenset(s.lower() for s in identity.ART_ONLY)
        if busy == getattr(self, "_lead_busy_seen", empty):
            return False
        self._lead_busy_seen = busy
        return True

    def _run_figures_drifted(self) -> bool:
        """Buy a board frame when a card's cost-and-time line moved.

        Executor only, at the end of `_reconcile_board` after
        `_observe_board_outcomes` has written the ledgers. Composes the same
        object the snapshot will publish and compares its `drift_key` per
        card with the last pass: the minute floor, the cent and the whole
        percent inside `compose` are what bound this to one frame per live
        card per minute rather than one per observation pass. `_manual_due_
        drifted`'s shape — a failure logs at debug and buys nothing.
        """
        try:
            if self._board is None:
                return False
            parts_by_card = self._board.run_figures()
            ctx_by_session = self._live_ctx_by_session(
                self._agents_snapshot_cache or {})
            links = {}
            for card in self._board.cards():
                cid = str(card.get("id") or "")
                if cid:
                    links[cid] = (str(card.get("link_state") or ""),
                                  str(card.get("session_id") or ""))
            seen = {}
            for cid, parts in parts_by_card.items():
                link, sid = links.get(cid, ("", ""))
                ctx = ctx_by_session.get(sid) if link == "live" else None
                seen[cid] = run_figures_mod.drift_key(run_figures_mod.compose(
                    cost=parts.get("cost") or {},
                    seconds=parts.get("seconds"),
                    ticking=bool(parts.get("ticking")),
                    ctx=ctx,
                    attempts=int(parts.get("attempts") or 0)))
            if seen == self._run_figures_seen:
                return False
            self._run_figures_seen = seen
            return True
        except Exception:
            logger.debug("run-figures drift check failed", exc_info=True)
            return False

    @staticmethod
    def _live_ctx_by_session(snapshot: dict) -> dict:
        """`{session_id: ctx_used_pct}` for every row in a live bucket.

        `_board_live_ids`' shape: the three live buckets **plus the
        finished rows flagged `alive`** — `_enrich_agent_stubs` moves a live
        session quiet past `FINISHED_IDLE_GRACE_SECONDS` into `finished`
        with `alive: True`, and without that union a still-live card would
        lose its context figure for the quiet spell and buy two frames for
        the flicker. A finished row without the flag is a tombstone and its
        last reading is not a live percent; the decorate joins this only
        where `link_state` is `live` anyway. Reads `metrics.ctx_used_pct`,
        the key `samples.py` already reads for the trend; a row without one
        contributes nothing.
        """
        out: dict = {}
        for bucket in ("running", "waiting", "sleeping", "finished"):
            for row in snapshot.get(bucket) or []:
                if not isinstance(row, dict) or not row.get("session_id"):
                    continue
                if bucket == "finished" and not row.get("alive"):
                    continue
                metrics_ = row.get("metrics")
                if not isinstance(metrics_, dict):
                    continue
                pct = metrics_.get("ctx_used_pct")
                if pct is None or isinstance(pct, bool):
                    continue
                try:
                    out[str(row["session_id"])] = float(pct)
                except (TypeError, ValueError):
                    continue
        return out

    def _needs_you_drifted(self, cards: list,
                           active: Optional[set] = None) -> bool:
        """Buy a board frame when hook freshness alone changes needs_you.

        Executor only, sharing the reconcile's active reading. Restrict the
        map to bound In progress cards so unrelated churn buys no frame.

        The mid-turn reading is taken here too, and it has to be: comparing
        on a bit the snapshot then decides differently would buy a frame for
        a change nobody draws, and — the way that costs something — miss the
        frame where a quiet session's tombstone expires and the card starts
        asking for a person.
        """
        try:
            mid_turn = self._mid_turn_quiet_ids()
            due = {}
            for card in cards:
                if (card.get("column_name") != "in_progress"
                        or not str(card.get("session_id") or "")):
                    continue
                cid = str(card.get("id") or "")
                if cid:
                    due[cid] = board_queue.needs_you(card, active, mid_turn)
            if due == self._needs_you_seen:
                return False
            self._needs_you_seen = due
            return True
        except Exception:
            logger.debug("needs-you drift check failed", exc_info=True)
            return False

    def _manual_due_drifted(self, cards: list, snapshot: dict,
                            active: Optional[set] = None) -> bool:
        """Whether any flagged card's `manual_check_due` moved since the last
        pass.

        The badge is derived from the agents snapshot, and a session's category
        flipping writes no board row — so without this check `_publish_board`
        (which fires only on `changed`) would leave the badge frozen at
        whatever the last board *write* published. Blocking; runs on the
        executor at the end of `_reconcile_board`.

        Restricted to cards carrying a note, and compared on the decided bit
        rather than on raw session membership: they are the only cards the bit
        draws anything on, so a quiet board still buys no frame.
        """
        try:
            running_ids = self._board_running_ids(snapshot)
            if active is None:
                active = self._claiming_session_ids()
            due = {}
            for card in cards:
                if not str(card.get("manual_steps") or "").strip():
                    continue
                cid = str(card.get("id") or "")
                if not cid:
                    continue
                due[cid] = not self._card_session_working(
                    card, running_ids, active)
            if due == self._manual_due_seen:
                return False
            self._manual_due_seen = due
            return True
        except Exception:
            logger.debug("manual-check drift check failed", exc_info=True)
            return False

    def _decide_queue_dispatches(self, cards: list,
                                 active: Optional[set] = None,
                                 snapshot: Optional[dict] = None) -> None:
        """Pick at most one startable card per project. Blocking; no side effects
        beyond `_queue_candidates`.

        A queued card held by an unmet dependency is skipped, never picked and
        never in the way (`board_queue.eligible`): the head is the first
        *eligible* card in `queue_key` order. `snapshot` is the agents
        snapshot the reconcile was handed, read for which sessions are
        working — the cached one where the caller has none — so a dependency
        flagged for a manual check reads as met here exactly when its badge
        lights.

        **One per project per pass**, which is enforced twice on purpose:
        binding by elimination needs "the first new session in this project" to
        be singular, and `dispatch.guard`'s own per-project in-flight refusal
        would turn a second into a transient refusal that merely holds. The
        cheap check is here; the correct one is still down there.

        No decision is remembered: this only names a card, and everything that
        makes starting it legal is re-read by `dispatch_card` at the instant it
        fires. Failure here costs the drain a pass, never the reconcile.
        """
        self._queue_candidates = []
        if not (self.board_dispatch_enabled and self.board_autostart_enabled):
            # The switches remove the decision, not the queue. With the drain
            # off a card stays queued in its order and simply waits for a
            # press; nothing here needs computing for it, because the sentence
            # it wears is composed from the running count at decoration time
            # rather than remembered from this pass.
            return
        try:
            queued = [c for c in cards
                      if str(c.get("queue_state") or "") == "queued"]
            if not queued:
                return
            if active is None:
                active = self._claiming_session_ids()
            running_ids = self._dependency_running_ids(
                snapshot if snapshot is not None
                else self._agents_snapshot_cache)
            held = self._dependency_held_ids(cards, running_ids, active)
            by_project: dict = {}
            for card in queued:
                by_project.setdefault(str(card.get("project") or ""),
                                      []).append(card)
            for project, rows in by_project.items():
                rows.sort(key=board_queue.queue_key)
                rows = board_queue.eligible(rows, held)
                if not rows:
                    continue
                running = len(board_queue.claims(cards, project, active))
                # The head is the card that would start, so its own root is
                # the one to resolve — never a label-to-root guess. Within a
                # project-label group the roots agree, an alignment
                # `relabel_root` and `_bind_dispatched_card`'s label refusal
                # maintain.
                head = board_queue.slot_head(
                    rows, running,
                    self._parallel_limit_for(rows[0].get("root")))
                if head is not None:
                    self._queue_candidates.append(str(head["id"]))
        except Exception:
            logger.debug("queue decision failed", exc_info=True)
            self._queue_candidates = []

    def _record_card_stages(self, card_id: str, session_id: str) -> bool:
        """Append observed stage names; recorded faces remain the store's memory."""
        state = self._session_states.get(session_id) or {}
        codex = self._codex_records.get(session_id)
        observed = codex.observed_roles if codex is not None else state.get("subagents_seen") or []
        names = [n for n in observed if self._stage_name_ok(n)]
        if not names:
            return False
        try:
            _, changed = self._board.record_agents(card_id, names, {})
        except Exception:
            logger.debug("could not record stages for %s", card_id, exc_info=True)
            return False
        return bool(changed)


    @staticmethod
    def _stage_name_ok(name: str) -> bool:
        """Whether a subagent name is a *stage* and not somebody's session id.

        `_parent_of_child` states the discriminator this relies on: Claude
        Code's `agent_id`s "are types (`bc-implementer`), which no session id
        can equal", while a Grok child is addressed by its own session id and is
        folded onto the parent as a subagent. Left alone, that child's uuid
        would become a marker on the card reading `a3f1c2d4…`, which says
        nothing to anyone.

        Deliberately conservative in one direction only: a real stage that
        happened to be uuid-shaped would be dropped, and a dropped marker is a
        track that is short by one. A uuid *drawn* is a track that is wrong.

        **The length was the bug.** This tested for 32+ hex characters — a
        uuid, and nothing shorter — while the harness that actually spawns
        these hands out ids of the shape `a5ef36af63236e492`: 17 hex
        characters, no dashes. Every one of them sailed through and was drawn
        on the card as a specialist, which is precisely the marker this
        function's own docstring says nothing to anyone. Measured on the
        live board: five of six cards carrying a trail showed nothing but hex.
        `OPAQUE_ID_MIN_CHARS` is the corrected floor, well above any real
        agent type (which needs letters outside `a-f` to reach that length)
        and well below the shortest id observed.
        """
        text = str(name or "").strip()
        if not text:
            return False
        bare = text.replace("-", "")
        if len(bare) >= _D.OPAQUE_ID_MIN_CHARS and all(
                c in "0123456789abcdefABCDEF" for c in bare):
            return False
        return True

    @staticmethod
    def _board_live_ids(snapshot: dict) -> set:
        """Every session in that snapshot which is still alive.

        The three live buckets, **plus the finished rows flagged `alive`**.
        `_enrich_agent_stubs` moves a live session quiet past
        `FINISHED_IDLE_GRACE_SECONDS` (120s) out of `sleeping` and into
        `finished` with `alive: True`; without that union a card whose agent is
        sitting in a terminal waiting for somebody would be stamped `ended`
        after the grace and the board would say the one thing it exists not to
        say. `_decide_auto_compacts` was caught by the same bucket and answers
        it the same way.
        """
        live = set()
        for bucket in ("running", "waiting", "sleeping"):
            for row in snapshot.get(bucket) or []:
                if isinstance(row, dict) and row.get("session_id"):
                    live.add(row["session_id"])
        for row in snapshot.get("finished") or []:
            if isinstance(row, dict) and row.get("session_id") and row.get("alive"):
                live.add(row["session_id"])
        return live

    @staticmethod
    def _board_running_ids(snapshot: dict) -> set:
        """Every session that snapshot files under `running`.

        A deliberately narrower sibling of `_board_live_ids`: that one asks
        *is this session still alive* and unions four buckets, this one asks
        *is it working right now* and reads the one bucket that says so. The
        buckets are `_reconciled_categories()`'s own reading, built on the loop
        thread with the waiting hysteresis already applied, so the badge and
        the rail's process table can never disagree about a session's state.

        Same defensive shape as its sibling — dict rows carrying an id only.
        """
        ids = set()
        for row in snapshot.get("running") or []:
            if isinstance(row, dict) and row.get("session_id"):
                ids.add(row["session_id"])
        return ids

    @staticmethod
    def _card_session_working(card: dict, running_ids: set,
                              active: set) -> bool:
        """Whether this card's assistant is *actively working* right now.

        The one predicate behind the manual-check badge, called from both
        consumers (the snapshot decoration and the reconcile's drift check) so
        the two cannot drift apart.

        `dispatching` counts as working: a re-Start on a flagged card is the
        assistant resuming, and hiding through the bind window stops the badge
        flashing while the terminal opens. A bind that fails clears the link
        and the badge comes back on its own.

        Two narrowings are deliberate. A session that is `waiting` or idle is
        **not** working — that is the badge's whole point, since a turn that
        ended on a question is exactly when somebody should do the hand-check.
        And a session in the `running` bucket but absent from
        `_claiming_session_ids()` is a roster-only stub — evicted from the hook
        stream, still listed busy by `claude agents --json` while its terminal
        stays open — so it is quiet, hence not working, hence badged. That
        wedge is decided in the badge's favour: a finished-and-quiet card is
        the one that most needs the line.
        """
        state = str(card.get("link_state") or "")
        if state == "dispatching":
            return True
        if state != "live":
            return False
        sid = str(card.get("session_id") or "")
        return bool(sid) and sid in running_ids and sid in active

    def _bindable_candidate(self, row: dict) -> bool:
        """Whether this snapshot row may be paired with a card at all.

        Only a *pipeline main agent* qualifies — an interactive top-level
        session of the kind Dark Army's own spawn produces. Two refusals, each
        killing a class of wrong pairing that used to be possible:

        - a row whose `kind` is not `interactive` — a background agent
          (`claude agents --json`, including one the dispatched session itself
          spawned) or an unnamed-kind roster row is never the session a Start
          opened. Hook rows without a record default to `interactive`, Grok
          reconciler rows hardcode it, and Codex roots report it, so every
          legitimate bind target passes.
        - a row that is really somebody's *subagent* — a stray Grok child
          firing hooks under its own id before the cleanup branch catches it.
          `_parent_of_child` is the existing discriminator: a row a parent
          already claims is never a session in its own right.
        """
        if (row.get("kind") or "") != "interactive":
            return False
        if self._parent_of_child(row.get("session_id") or ""):
            return False
        return True

    def _rename_for_role(self, session_id: str, card_id: str,
                         kind: str, area: str) -> None:
        """Correct a just-bound session's nickname to its role's character.

        The one call site of `IdentityStore.reassign`, and the reason it
        exists. `_assign_nicknames` names a session on the push that first
        sees it, and `_reconcile_board` — where this runs — is a frame behind
        that, so a dispatched session can appear once under a hashed name
        before Dark Army knows what it is for. This corrects it, once, at the bind.

        Executor-only: both binders run inside `_reconcile_board`, itself a
        `run_in_executor` from `_push_agents_snapshot`. It mutates only
        `IdentityStore._sessions` and sets `_dirty`; the disk write stays where
        it is, `self._identities.save()` at the end of `_assign_nicknames` on
        the next push.

        The busy set is every *shown* session's name **as the identity store
        currently holds it**, minus this session's own. `_nicknames_shown` is
        the roster of sessions on screen, written by `_assign_nicknames`
        earlier in the same push — but it is never written back to by a
        rename, so reading its values directly would miss a rename this very
        `_reconcile_board` pass already made and hand two sessions bound in
        one pass the same character. `assigned()` does reflect it, which is
        the whole reason the busy set is read through the store.
        A `""` answer (no pool, or every member of it busy) leaves the hash
        name alone.
        """
        if not session_id:
            return
        shown = getattr(self, "_nicknames_shown", {}) or {}
        assigned = self._identities.assigned()
        current = assigned.get(session_id) or shown.get(session_id) or ""
        taken = {assigned.get(sid) or name
                 for sid, name in shown.items() if sid != session_id}
        taken.discard("")
        name = self._role_nickname(kind, card_id, area, taken)
        if not name or name == current:
            return
        if self._identities.reassign(session_id, name):
            logger.info("card %s: renamed %s to %s for %s",
                        card_id[:8], session_id[:12], name, kind)

    def _candidate_matches(self, row: dict, *, baseline: set, tool: str,
                           project: str, root: str, since: float,
                           shell_pid, pty_pid=None) -> str:
        """Whether this snapshot row may be the session a spawn produced.

        The one predicate behind all three binders — `_bind_dispatched_card`,
        `_bind_refining_card` and `_consult_accepts` — so the join cannot
        drift apart between them (it had: a pid miss under a terminal receipt
        was a refusal in the consult binder and a skip in the other two).

        Three verdicts rather than a bool, because a pid miss is its own
        state: `"match"`, `"no"`, or `"unproven"` — the row passed every test
        except the terminal-receipt proof (`shell_pid` set, the row's pid
        missing or not descended from it). Unproven is a **skip within the
        window**, never a refusal: the row may gain its pid on the next
        snapshot, and the binders use the verdict only to word their give-up.
        """
        if not isinstance(row, dict):
            return "no"
        sid = row.get("session_id") or ""
        if not sid or sid in (baseline or set()):
            return "no"
        if not self._bindable_candidate(row):
            return "no"
        if (row.get("provider") or "claude") != tool:
            return "no"
        if (row.get("project") or "") != (project or ""):
            return "no"
        cwd = row.get("cwd") or ""
        if cwd and root:
            resolved = dispatch.normalise_root(cwd)
            if resolved != root and not resolved.startswith(root + os.sep):
                return "no"
        started = row.get("started_at")
        if isinstance(started, (int, float)) and started + 5.0 < since:
            return "no"
        if shell_pid:
            pid = row.get("pid")
            if not pid or not _D._pid_descends(pid, shell_pid):
                return "unproven"
        if pty_pid:
            # Dark Army's own terminal: Dark Army is the parent and holds the child pid
            # exactly, so the proof is an identity (or descent from it, for
            # a CLI that re-execs itself). Its own term rather than a second
            # `shell_pid`, because the two maps mean different things.
            pid = row.get("pid")
            if not pid or not _D._pid_descends(pid, pty_pid):
                return "unproven"
        return "match"

    def _bind_dispatched_card(self, card: dict, snapshot: dict,
                              now: float) -> bool:
        """Find the session a Start produced, or give up on it. Blocking.

        By elimination, which is the only join available: the spawn goes through
        VS Code and returns a terminal name, not a session id. A candidate must
        be a *pipeline main agent* (`_bindable_candidate` — interactive,
        top-level, never a background agent or somebody's subagent), new since
        the press, of the card's provider, in the card's project, and started
        after the press. The per-project concurrency bound in `dispatch.guard`
        is what makes "the first such session" a single session.

        With a terminal receipt (`_spawn_shell_pids`, the shell pid the
        extension reported for the terminal it opened), elimination becomes
        proof: only a session whose pid descends from that terminal binds. A
        row without a pid yet, or with the wrong one, is *skipped* — it may
        gain its pid on the next snapshot inside the window — and a window
        that closes with only unprovable candidates writes its own give-up
        reason, so a tightened match is tellable from a dispatch that produced
        nothing. Pid reuse inside the window could theoretically satisfy the
        descent check; bounded and accepted (see `_pid_descends`).

        The hand-started residual survives **only** in the no-receipt fallback
        (a window on an extension older than 0.1.10, `processId` unresolved,
        or Dark Army restarted mid-window): there a user's own session in the same
        project during the window can still be bound. The cost of the
        tightened rule is the other way — more cards timing out unbound, each
        recovered by `board_reset` or a second Start, with the reason on the
        orange `dispatch_error` line.
        """
        cid = card.get("id") or ""
        dispatched_at = card.get("dispatched_at") or 0.0
        baseline = self._dispatch_baseline.get(cid, set())
        root = dispatch.normalise_root(card.get("root") or "")
        shell = self._spawn_shell_pids.get(cid)
        pty_pid = self._spawn_pty_pids.get(cid)
        proof_refused = False
        # **The window is judged once, before the scan, and it gates the
        # bind.** The scan still runs — an unprovable row is what words the
        # give-up below — but past the window nothing may be *paired*: the
        # join is elimination ("the first new session in this project"), so a
        # pass arriving long after the press, which is what a slept machine
        # or a reconcile that stopped and was restarted produces, would
        # otherwise hand the card whichever terminal somebody happened to
        # open since. Late means give up, never bind.
        expired = now - dispatched_at > dispatch.DISPATCH_BIND_WINDOW
        for bucket in ("running", "waiting", "sleeping"):
            for row in snapshot.get(bucket) or []:
                verdict = self._candidate_matches(
                    row, baseline=baseline, tool=card.get("tool"),
                    project=card.get("project") or "", root=root,
                    since=dispatched_at, shell_pid=shell, pty_pid=pty_pid)
                if verdict == "unproven":
                    proof_refused = True
                    continue
                if verdict != "match":
                    continue
                if expired:
                    # A session that turned up after the window is not this
                    # card's. Fall through to the give-up.
                    continue
                sid = row.get("session_id") or ""
                # Done is kept by the store as of 2026-08-24.
                bound, _detail = self._board.bind_session(cid, sid)
                if bound is None:
                    # Card gone (deleted mid-bind). Leave the receipt so
                    # delete can still close the terminal Dark Army opened.
                    return False
                self._record_outcome_binding(
                    bound, sid, "implementation", late=not bool(shell or pty_pid))
                self._rename_for_role(sid, cid, "start", str(bound.get("area") or ""))
                self._dispatch_baseline.pop(cid, None)
                self._spawn_shell_pids.pop(cid, None)
                self._spawn_pty_pids.pop(cid, None)
                if pty_pid:
                    # Name the pty after the session it turned out to be, so
                    # the panel's terminal read resolves by session id.
                    handle = self._pty.owns(pty_pid)
                    if handle is not None:
                        self._pty.bind(handle, sid)
                logger.info("card %s bound to session %s", cid[:8], sid[:12])
                return True

        if not expired:
            return False
        current = self._board.get(cid) or card
        # Judged while the card is still `dispatching`: the writes below
        # clear its link, and a batch whose one binding card has let go
        # has no owner left to ask.
        owns_batch = self._batch_owned_by(current)
        if str(current.get("column_name") or "") == "done":
            # A Done card that never bound stays in Done. The bind no
            # longer matters, so there is no orange `dispatch_error`.
            self._board.update(cid, {
                "link_state": "",
                "dispatched_at": None,
                "dispatch_error": "",
            })
            self._release_expired_batch(card, owns_batch)
        else:
            # Nothing appeared. Back to the column the card's own state names, with
            # the reason written on it — and the terminal is left exactly where it
            # is: Dark Army opened it and may not understand what happened in it, and
            # closing somebody's terminal on a guess is worse than a card that says
            # so. The column is read off `plan_path` rather than remembered from
            # the press: a card with no plan goes to **Prep** (its caption — just
            # written, not yet planned — is the honest description of it; Backlog's
            # caption promises "planned", and a card started unplanned from Prep
            # landing there would falsify the one-line column meaning that is the
            # whole argument for Prep). Stateless on purpose: an origin remembered
            # in a dict would be lost across a daemon restart, and `plan_path`
            # says the same thing without the bookkeeping.
            error = (
                "a session appeared but Dark Army could not prove it was the one it "
                "started — check the terminal that opened"
                if proof_refused else
                dispatch.first_run_hint(card.get("tool"))
                or "no session appeared — check the terminal that opened")
            # `bump=False`, the dispatch write's own reason: sending a card
            # back because no session appeared is Dark Army's bookkeeping, not a
            # person changing a word on it.
            self._board.update(cid, {
                "column_name": "backlog" if str(card.get("plan_path") or "")
                else "prep",
                "link_state": "",
                "dispatched_at": None,
                "dispatch_error": error,
            }, bump=False)
            self._release_expired_batch(card, owns_batch)
            # On the executor: `_log_event` appends directly here.
            self._log_card_event(card, "card_dispatch_failed", error=error)
        self._dispatch_baseline.pop(cid, None)
        self._spawn_shell_pids.pop(cid, None)
        self._spawn_pty_pids.pop(cid, None)
        logger.info("card %s gave up waiting for its session", cid[:8])
        return True

    def _batch_owned_by(self, card: Optional[dict]) -> bool:
        """Whether this card speaks for its batch: it is the batch's owner
        member (`_batch_owner`), or it is bound to that member's session.
        Blocking (one store read); executor.

        The gate on every path that releases a batch's waiting members — the
        session ending, the bind window expiring, a reset or a delete — so a
        member dragged out of Backlog and started alone can never end the
        batch it was taken from."""
        card = card or {}
        bid = str(card.get("batch_id") or "")
        if not bid or self._board is None:
            return False
        head = _batch_owner(self._board.batch_members(bid))
        if head is None:
            return False
        if str(head.get("id") or "") == str(card.get("id") or ""):
            return True
        sid = str(card.get("session_id") or "")
        return bool(sid) and str(head.get("session_id") or "") == sid

    def _batch_has_owner(self, batch_id: str) -> bool:
        """Whether any member still speaks for this batch. Blocking;
        executor. Asked after a reset or delete took a card out of it: a
        batch nobody owns can never be walked, so its waiting members go
        back rather than wait for ever."""
        if not batch_id or self._board is None:
            return False
        return _batch_owner(self._board.batch_members(batch_id)) is not None

    def _release_expired_batch(self, card: dict, owns: bool) -> None:
        """The head of a batch-implement press never bound: its waiting
        members go back to plain Backlog with `BATCH_LEFT_NOTE`, and the head
        leaves the batch. Blocking; executor. A card with no mark is
        untouched, so a single Start's give-up is exactly what it was."""
        bid = str((card or {}).get("batch_id") or "")
        if not bid or self._board is None:
            return
        # The head leaves the batch **first**: with its link already cleared
        # it would otherwise match the release's WHERE and have its own
        # give-up reason overwritten with the members' note.
        self._board.update(str(card.get("id") or ""),
                           {"batch_id": "", "batch_rank": ""}, bump=False)
        if owns:
            self._board.release_batch_waiting(bid, BATCH_LEFT_NOTE)

    def _bind_refining_card(self, card: dict, snapshot: dict,
                            now: float) -> bool:
        """Find the session a Refine produced, or give up on it. Blocking.

        `_bind_dispatched_card`'s elimination — the same
        `_bindable_candidate` predicate and the same terminal-receipt proof —
        with two deliberate differences. The provider match is
        `card.get("tool")`, the same field Start already matches, so a Grok
        plan is not glued onto a Claude window that happened to appear. And
        the write is `refine_session_id` + `refine_state="live"`, **never**
        `bind_session`: that verb moves the card to In progress, and the whole
        reason the refine fields exist is that a refining card stays in Prep.
        """
        cid = card.get("id") or ""
        dispatched_at = card.get("dispatched_at") or 0.0
        baseline = self._refine_baseline.get(cid, set())
        root = dispatch.normalise_root(card.get("root") or "")
        shell = self._spawn_shell_pids.get(cid)
        pty_pid = self._spawn_pty_pids.get(cid)
        proof_refused = False
        # `_bind_dispatched_card`'s rule, verbatim: the scan still words the
        # give-up, but past the window nothing may be paired.
        expired = now - dispatched_at > dispatch.DISPATCH_BIND_WINDOW
        for bucket in ("running", "waiting", "sleeping"):
            for row in snapshot.get(bucket) or []:
                verdict = self._candidate_matches(
                    row, baseline=baseline, tool=card.get("tool"),
                    project=card.get("project") or "", root=root,
                    since=dispatched_at, shell_pid=shell, pty_pid=pty_pid)
                if verdict == "unproven":
                    proof_refused = True
                    continue
                if verdict != "match":
                    continue
                if expired:
                    continue
                sid = row.get("session_id") or ""
                self._board.update(cid, {
                    "refine_session_id": sid,
                    "refine_state": "live",
                })
                self._record_outcome_binding(
                    card, sid, "refinement", late=not bool(shell or pty_pid))
                self._rename_for_role(sid, cid, "refine", str(card.get("area") or ""))
                self._refine_baseline.pop(cid, None)
                self._spawn_shell_pids.pop(cid, None)
                self._spawn_pty_pids.pop(cid, None)
                if pty_pid:
                    # Name the pty after the session it turned out to be, so
                    # the panel's terminal read resolves by session id.
                    handle = self._pty.owns(pty_pid)
                    if handle is not None:
                        self._pty.bind(handle, sid)
                logger.info("card %s refinement bound to session %s",
                            cid[:8], sid[:12])
                return True

        if not expired:
            return False
        # Nothing appeared. The card stays in Prep with the reason written on
        # it — the orange line the board already draws — and the terminal, if
        # one opened, is left alone for `_bind_dispatched_card`'s reason.
        error = (
            "a session appeared but Dark Army could not prove it was the "
            "refinement it started — check the terminal that opened"
            if proof_refused else
            dispatch.first_run_hint(card.get("tool"), "Refine")
            or "the refinement session never appeared")
        self._board.update(cid, {
            "refine_state": "",
            "dispatched_at": None,
            "dispatch_error": error,
            # A batch member that never bound leaves its batch with the link.
            "batch_id": "",
            "batch_rank": "",
        })
        self._log_card_event(card, "card_dispatch_failed", error=error)
        self._refine_baseline.pop(cid, None)
        self._spawn_shell_pids.pop(cid, None)
        self._spawn_pty_pids.pop(cid, None)
        logger.info("card %s gave up waiting for its refinement", cid[:8])
        return True

    def _bind_consults(self, snapshot: dict, now: float) -> bool:
        """Bind helper sessions by elimination, or give up on them. Blocking.

        Same join as `_bind_dispatched_card` (new since the press, provider
        claude, card's project, cwd under root, started after), writing the
        winner into the consult entry only, never into the card. Past
        `DISPATCH_BIND_WINDOW` with no session: drop the entry and leave a
        note; the terminal is left alone. A bound helper that has left the
        snapshot is dropped the same way — it must not occupy `_consults`
        forever, even though it no longer occupies the launch bound.
        """
        if self._board is None:
            return False
        live = self._board_live_ids(snapshot)
        changed = False
        for cid, entry in list(self._consults.items()):
            sid = entry.get("session_id") or ""
            if sid:
                if sid not in live:
                    self._drop_consult(
                        cid, "the helper left without answering")
                    changed = True
                continue
            if self._bind_one_consult(cid, entry, snapshot, now):
                changed = True
        return changed

    def _consult_accepts(self, entry: dict, row: dict) -> bool:
        """Whether this snapshot row may be this consult's helper right now.

        `_candidate_matches`, the same predicate the two card binders use. An
        `"unproven"` row (a pid miss under a terminal receipt) is False here,
        which the bind loop treats as skip-and-retry within the window —
        unified with the binders, where the same verdict is the same skip.
        """
        return self._candidate_matches(
            row, baseline=entry.get("baseline") or set(), tool="claude",
            project=entry.get("project") or "",
            root=dispatch.normalise_root(entry.get("root") or ""),
            since=float(entry.get("started") or 0.0),
            shell_pid=entry.get("shell_pid")) == "match"

    def _snapshot_row_for(self, session_id: str):
        """The last agents-snapshot row for this session, or None."""
        if not session_id:
            return None
        for group in (self._agents_snapshot_cache or {}).values():
            if not isinstance(group, list):
                continue
            for row in group:
                if isinstance(row, dict) and row.get("session_id") == session_id:
                    return row
        return None

    def _drop_consult(self, cid: str, note: str) -> None:
        self._consults.pop(cid, None)
        if not note or self._board is None:
            return
        try:
            self._board.add_message(cid, "bob", note, "note", "")
        except Exception:
            logger.debug("could not note a vanished consult on %s", cid,
                         exc_info=True)

    def _bind_one_consult(self, cid: str, entry: dict, snapshot: dict,
                          now: float) -> bool:
        started_at = float(entry.get("started") or 0.0)
        for bucket in ("running", "waiting", "sleeping"):
            for row in snapshot.get(bucket) or []:
                if not isinstance(row, dict):
                    continue
                if not self._consult_accepts(entry, row):
                    continue
                sid = row.get("session_id") or ""
                entry["session_id"] = sid
                logger.info("consult for card %s bound to session %s",
                            cid[:8], sid[:12])
                return True

        if now - started_at <= dispatch.DISPATCH_BIND_WINDOW:
            return False
        self._drop_consult(
            cid, "no helper session appeared — check the terminal that opened")
        logger.info("card %s gave up waiting for its helper", cid[:8])
        return True

    async def answer_card_by_session(self, session_id: str, text: str) -> tuple:
        """Store an answer from this session onto the card it is answering.

        Ladder, fail closed on ambiguity: (a) a consult whose bound session
        is the caller, `via=consultant`, retire the consult; (b) exactly one
        non-Done card via `by_session`, `via=session`; (c) exactly one Done
        card via `by_session`. Writes `card_messages` only.
        """
        if self._board is None:
            return None, "the board is not open"
        if not session_id:
            return None, "Dark Army could not tell which session asked"
        text = str(text or "").strip()
        if not text:
            return None, "an answer needs some text"

        consult_hits = [cid for cid, entry in self._consults.items()
                        if entry.get("session_id") == session_id]
        if not consult_hits:
            # A helper that answers on the first turn, before `_bind_consults`
            # stamped it, is still this consult — match the unbound entries
            # against the same predicates the bind uses.
            row = self._snapshot_row_for(session_id)
            if row is not None:
                consult_hits = [
                    cid for cid, entry in self._consults.items()
                    if not entry.get("session_id")
                    and self._consult_accepts(entry, row)]
        if len(consult_hits) > 1:
            return None, ("this session is on more than one card — "
                          "say which on the board")
        if len(consult_hits) == 1:
            cid = consult_hits[0]
            entry = self._consults.get(cid) or {}
            if not entry.get("session_id"):
                entry["session_id"] = session_id
            msg, detail = await self._board_call(
                "add_message", cid, session_id, text, "answer", "consultant")
            if msg is None:
                still = await self._board_call("get", cid)
                if still is None:
                    self._consults.pop(cid, None)
                return None, detail
            self._consults.pop(cid, None)
            if entry.get("purpose") == "review":
                # A review's answer: the verdict off its first line, with
                # the branch version it judged.
                await self._record_review_from_answer(cid, entry, text)
            await self._publish_board()
            return (await self._board_call("get", cid)), "answered"

        cards = await self._board_call("by_session", session_id) or []
        open_cards = [c for c in cards if c.get("column_name") != "done"]
        if len(open_cards) > 1:
            return None, ("this session is on more than one card — "
                          "say which on the board")
        if len(open_cards) == 1:
            target = open_cards[0]
        else:
            done_cards = [c for c in cards if c.get("column_name") == "done"]
            if len(done_cards) == 1:
                target = done_cards[0]
            elif not cards:
                return None, "no card on Dark Army's board names this session"
            else:
                return None, ("this session is on more than one card — "
                              "say which on the board")
        msg, detail = await self._board_call(
            "add_message", target["id"], session_id, text, "answer", "session")
        if msg is None:
            return None, detail
        await self._publish_board()
        return target, "answered"

    async def _handle_board_card_request(self, msg: dict) -> dict:
        """A session asking, through Dark Army's channel, to put a card on the board.

        **The hook socket authenticates nothing** — `_attach_is_displaced` says
        so in its own docstring, and it takes JSON from anything on this machine
        that can reach the port. So the port, pid and session id here are merely
        *claimed*, and three things follow, all of them load-bearing:

        * The session is resolved with `_channel_session(port)` — by pid at the
          moment of use, never from a field in the message — and the request is
          **refused outright** if it does not resolve. An unattributable card is
          the forgery case, and a card with no author is exactly what somebody
          forging one would want to leave.
        * The card lands in `prep`, always, and **can start nothing**. There
          is no dispatch tool, the tool's schema has no column at all any more,
          and `dispatch_card` has one caller. That is what makes an
          unauthenticated create tolerable: the worst a forged message achieves
          is a card a human has to read and then drag across the board.
        * A `project` the caller names must resolve to a known root; otherwise
          the card takes the *calling session's own* project, which is the only
          project the caller has any standing to speak for.
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}

        own_cwd, own_project = self._session_place(session_id)
        author_guard = self._board_author_guard(port)
        if not own_cwd:
            # The channel process states its own working directory when it
            # attaches, and it is the session's own — closer to the truth than
            # anything derived, and there a snapshot tick before the row is.
            # It widens nothing: a claimed folder still has to be a project Dark Army
            # can see (`create_card` drops a root outside `known`), which is the
            # same test a caller-named project passes.
            own_cwd = str((self._channels.get(port) or {}).get("cwd") or "")
        loop = asyncio.get_running_loop()
        known = await loop.run_in_executor(None, self._known_project_roots)
        # The window labels, so a card filed by an agent is filed under the same
        # name every other card uses. Matching on `os.path.basename` alone and
        # storing *that* put the same project on the board twice — once as the
        # workspace label and once as the folder name — which is the exact
        # duplication `workspace.py` exists to stop.
        labels = await loop.run_in_executor(None, self._board_projects)
        by_root = {p["root"]: p["name"] for p in labels if p.get("root")}

        root = ""
        project = str(msg.get("project") or "").strip()
        if project:
            # A named project only counts if Dark Army can see it — the worst a
            # caller can do by inventing a name is have the card land in its own.
            root = self._root_for_project_name(project, labels, known)
            if not root:
                project = ""
        if not project:
            root = dispatch.normalise_root(own_cwd) if own_cwd else ""
            # A session working in a card folder (`<root>/.worktrees/card-…`)
            # files for the main checkout, never for its own folder: that
            # folder goes when the card is released, and every card naming
            # it is then refused with "no open window for that project"
            # (28 Sep 2026, eight cards). `_known_project_roots` admits the
            # checkout of every live session's card folder.
            root = dispatch.normalise_root(worktrees.checkout_root(root))
            # A session working *inside* a project — `dark-army/host` — has a
            # cwd that is not itself a known root, and the payload below would
            # drop it to `""` after the name fallback had already been skipped.
            # Dropped here instead, so the name still gets its turn.
            if root not in known:
                root = ""
            project = own_project or (os.path.basename(root) if root else "")
        if not root and project:
            # **The name is evidence too.** A session can be in the snapshot with
            # its workspace resolved and no `cwd` on the row, and that is how a
            # card ends up in the right column, under the right heading, with an
            # empty `root` — which `dispatch.guard` then refuses with "this card
            # does not say which folder to work in". The project name is Dark Army's
            # own resolution (`workspace.py`), not the caller's claim, and it is
            # matched against the same visible-projects list a named project is,
            # so this widens nothing: it recovers the folder Dark Army already knows
            # the session is in.
            root = self._root_for_project_name(project, labels, known)
        # Whatever route got us here, the stored name is the workspace label when
        # there is one.
        project = by_root.get(root, project)

        # What the caller expects to run. Codex synthesises its own stage
        # names, and losing the whole note an agent was trying to leave because
        # one of them is a stranger is a worse outcome than a shorter list —
        # so these go to `create_card` as they came, and its own filter drops
        # what this project does not declare. An unknown root or an unreadable
        # roster filters nothing there, so nothing is lost on either.
        stages = board.parse_stages(msg.get("stages"))

        # A finished plan, filed with its card in one call. Validated before
        # anything is written, against the root the card will carry, so a
        # bad path refuses the whole request and leaves no half-filed card.
        # This is the route a planning session that writes several plans
        # needs: `attach_plan_by_session`'s authored-card rung fails closed on
        # a second unplanned Prep card, and a session that had filed one
        # follow-up note could attach none of its later plans — they landed
        # in Prep with the plan named only in the notes, and pressing Start
        # there opened a *planning* run that nothing could ever finish.
        plan_text = str(msg.get("plan") or "").strip()
        resolved_plan = ""
        if plan_text:
            card_root = root if root in known else ""
            resolved_plan, refusal = await loop.run_in_executor(
                None, self._plan_path_refusal, card_root, plan_text)
            if refusal:
                return {"ok": False, "detail": refusal}

        card, detail = await self.create_card({
            "title": str(msg.get("title") or ""),
            # The plain-language line, if the caller wrote one. It is the half of
            # the card a person actually reads, so an agent that can describe its
            # own suggestion in a sentence should be asked to.
            "summary": str(msg.get("summary") or ""),
            "prompt": str(msg.get("notes") or ""),
            "project": project,
            "root": root if root in known else "",
            "tool": str(msg.get("tool") or ""),
            # Prep, unconditionally — `dark_army_add_card`'s schema still names no
            # column at all; only the default moved when Prep arrived. A card
            # is a note for a person; where it goes next is the person's
            # gesture (or `dark_army_attach_plan`, which can only ever reach
            # Backlog), and In progress is a dispatch.
            "column_name": "prep",
            # Annotation only, `stages`' own argument: the store normalises
            # `"ship"` onto `''` and refuses a stranger. A scout still lands
            # in Prep and starts nothing.
            "kind": str(msg.get("kind") or ""),
            # Attribution, and the reason the refusal above is outright.
            "author": session_id,
            # What the caller expects to run. Annotation only: it decides which
            # markers the card draws as still-to-come and grants nothing. The
            # store normalises and bounds it, and the roster filter above has
            # already dropped anything this project does not declare — so a
            # caller that sends nonsense still gets a shorter list rather than
            # a refused card.
            "workflow": stages,
        }, _author_guard=author_guard)
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s added board card %s", session_id[:12],
                    card["id"][:8])
        plan_detail = ""
        if resolved_plan:
            # To this card by id — the one just created, never a lookup — so
            # no ladder and no ambiguity. The store's own WHERE (Prep, no
            # plan yet) still guards it. A fresh card has no
            # `start_when_planned`, so nothing is scheduled to start.
            attached, attach_detail = await self._board_call(
                "attach_plan", card["id"], resolved_plan, session_id)
            if attached is not None:
                card = await self._seed_from_plan(attached, resolved_plan)
                self._log_card_event(
                    card, "card_plan_attached", session_id=session_id,
                    plan_path=os.path.basename(resolved_plan))
            else:
                plan_detail = attach_detail or "the plan was not attached"
            await self._publish_board()
        # The cards this one must wait for, for a card filed **without** a
        # plan (a follow-up, a Prep note) — a planned card takes them from
        # its `Depends on:` header above. Ids or exact titles within the
        # card's own project, resolved here after the create; a reference
        # that names nothing or two cards refuses the whole list, and the
        # card is filed regardless: the reply says what went wrong in
        # `dependencies_detail`, `plan_detail`'s shape. Written through the
        # ordinary `update`, so the store's cycle, self and same-project
        # refusals apply exactly as they do to a person's edit.
        dependencies_detail = ""
        refs = msg.get("depends_on")
        if isinstance(refs, str):
            refs = [refs]
        if isinstance(refs, (list, tuple)):
            refs = [str(r)[:MAX_DEPENDENCY_REF_CHARS] for r in refs
                    if isinstance(r, (str, int)) and str(r).strip()]
            refs = refs[:board.MAX_BLOCKERS]
        else:
            refs = []
        if refs:
            ids, refusal = await loop.run_in_executor(
                None, functools.partial(
                    self._resolve_dependency_refs, refs,
                    str(card.get("root") or ""), str(card["id"])))
            if refusal:
                dependencies_detail = refusal
            elif ids:
                linked, detail = await self._board_call(
                    "update", card["id"], {"blocked_by": board.join_ids(ids)})
                if linked is None:
                    dependencies_detail = detail or "the links were not saved"
                else:
                    card = linked
                    await self._publish_board()
        return {"ok": True, "detail": "added to the board",
                "card_id": card["id"], "column": card["column_name"],
                "project": card["project"],
                "plan_attached": bool(card.get("plan_path")),
                "plan_detail": plan_detail,
                "dependencies_detail": dependencies_detail}

    def _knowledge_reach(self, cwd: str) -> tuple:
        """`(known roots, the enrolled root containing cwd)`. Both blocking
        (a ledger read and a snapshot walk), so **executor only** — asked in
        one hop because `_known_project_roots` already calls `root_enrolled`
        per root and a second hop would read the ledger twice."""
        return (self._known_project_roots(),
                enrollment.root_enrolled(cwd) if cwd else "")

    async def _knowledge_place(self, msg: dict) -> tuple:
        """`(root, refusal)` for a knowledge verb. Exactly one of them is set.

        `_handle_board_card_request`'s opening, with the card path's one
        widening removed: **a named project is not consulted, because there is
        no way to name one.** Neither schema declares `project`, `root` or
        `session_id`, this reads none of those keys off the message even if a
        forger put them there, and the only thing that decides which project's
        notes are reachable is where Dark Army already believes the calling session
        is working.

        **Reachability and identity are two different questions, and the notes
        are keyed on the second.** `_known_project_roots()` is the folders of
        every open VS Code window ∪ the cwd of every live session, so a session
        started in `<proj>/host` — which is this repo's own instructions —
        is a member of it in its own right. Keying on that cwd would give the
        subdirectory its own bucket: a session at the project root would then
        read `entries: []` and the skill would re-ask every question somebody
        had already answered. So membership is still tested on the **cwd**,
        leaving reach exactly as it was, and what is *stored* is
        `enrollment.root_enrolled(cwd)` — the longest enrolled root containing
        it, which is the one thing every session in a project agrees on. An
        unenrolled cwd resolves to `""` and is refused; the door above has
        already admitted the message, so this cannot be the only enrolment
        test, and it is not meant to be.

        The rest is the card path's ladder: resolve the session from `port` by
        pid at the moment of use and refuse if it does not resolve; take
        `_session_place`, falling back to the channel's own attached `cwd` (the
        session's own statement of where it is, and there a snapshot tick
        before the row is).
        """
        port = int(msg.get("port") or 0)
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return "", "Dark Army could not tell which session asked"
        if self._board is None:
            return "", _D.KNOWLEDGE_STORE_REFUSAL
        own_cwd, _own_project = self._session_place(session_id)
        if not own_cwd:
            own_cwd = str((self._channels.get(port) or {}).get("cwd") or "")
        here = dispatch.normalise_root(own_cwd) if own_cwd else ""
        loop = asyncio.get_running_loop()
        known, root = await loop.run_in_executor(
            None, self._knowledge_reach, here)
        if not here or here not in known or not root:
            return "", _D.KNOWLEDGE_UNPLACED_REFUSAL
        return root, ""

    async def _handle_knowledge_read_request(self, msg: dict) -> dict:
        """A session asking, through Dark Army's channel, for its project's notes.

        The message carries no project and this reads none: see
        `_knowledge_place`. The reply is this root's rows and nothing else —
        `knowledge_for` has no "all roots" mode to reach for.
        """
        root, refusal = await self._knowledge_place(msg)
        if refusal:
            return {"ok": False, "detail": refusal}
        entries = await self._board_call("knowledge_for", root)
        if entries is None:
            # `_board_call` answers `None` for a board that closed between
            # `_knowledge_place`'s check and this read. "Dark Army could not read"
            # and "this project has answered nothing" must not reach the model
            # as the same sentence — the second would have the skill ask every
            # question again.
            return {"ok": False, "detail": _D.KNOWLEDGE_STORE_REFUSAL}
        return {"ok": True, "entries": entries}

    async def _handle_knowledge_write_request(self, msg: dict) -> dict:
        """A session filing one answer into its project's notes.

        Same placement, same refusals; the store does the clamping and the
        bounds, and its `(ok, detail)` is returned verbatim so the model reads
        Dark Army's own words rather than a paraphrase of them.
        """
        root, refusal = await self._knowledge_place(msg)
        if refusal:
            return {"ok": False, "detail": refusal}
        session_id = (await self._board_request_session_fresh(
            int(msg.get("port") or 0)) or "")
        result = await self._board_call(
            "knowledge_put", root,
            # `note_key`, never `key`: `key` on this socket is the
            # project's enrolment key and `_enrolled_root` reads it.
            str(msg.get("note_key") or ""),
            str(msg.get("question") or ""),
            str(msg.get("answer") or ""),
            session_id)
        if result is None:
            return {"ok": False, "detail": _D.KNOWLEDGE_STORE_REFUSAL}
        ok, detail = result
        return {"ok": bool(ok), "detail": detail}

    def _knowledge_enrolled_root(self, root: str) -> str:
        """The named root as an exact enrolled member, or raise ValueError.

        Empty is refused in words, never `knowledge_for("")` which would
        return `[]` and read as "this project has answered nothing". The
        named path must be an exact member of `enrollment.enrolled_roots()`
        after `enrollment.normalise`; `root_enrolled` is the belt that the
        same path still resolves to itself.
        """
        raw = str(root or "").strip()
        if not raw:
            raise ValueError("Dark Army needs an enrolled project")
        try:
            normalised = enrollment.normalise(raw)
        except (OSError, ValueError):
            normalised = ""
        if not normalised:
            raise ValueError("Dark Army needs an enrolled project")
        if normalised not in enrollment.enrolled_roots():
            raise ValueError("Dark Army is not watching that project")
        if enrollment.root_enrolled(normalised) != normalised:
            raise ValueError("Dark Army is not watching that project")
        return normalised

    def _knowledge_report_sync(self, root: str) -> dict:
        normalised = self._knowledge_enrolled_root(root)
        if self._board is None:
            return {"supported": True, "available": False,
                    "root": normalised, "entries": [],
                    "truncated": False, "omitted_keys": []}
        entries = self._board.knowledge_for(normalised)
        return {"supported": True, "available": True,
                "root": normalised, "entries": list(entries or []),
                "truncated": False, "omitted_keys": []}

    async def knowledge_report(self, root: str) -> dict:
        """Human reader for one enrolled project's notes. No all-roots path."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._knowledge_report_sync, root)

    # ── The Checks section: manual-check files across enrolled projects ──

    #: The status filter's words; `""` and `all` keep every check.
    MANUAL_CHECK_FILTERS = ("", "all") + manual_check.STATUSES

    @staticmethod
    def _manual_check_place(path: str) -> tuple:
        """`(resolved, refusal)` for a check file a person names from a
        client. **Blocking** — executor only. **The security boundary of the
        outcome verb**: the path arrives over the sealed doors, so it must
        be absolute, name a regular file that is not a symlink in a dated
        folder that is not a symlink, and realpath to
        `<enrolled root>/manual-check/<folder>/check.md`, and the file must
        pass `manual_check.check`. Anything else is refused in words and
        nothing is read further."""
        text = str(path or "").strip()
        if not text or not os.path.isabs(text):
            return "", board.MANUAL_CHECK_PLACE_REFUSAL
        try:
            if not stat_mod.S_ISREG(os.lstat(text).st_mode):
                return "", board.MANUAL_CHECK_PLACE_REFUSAL
            if not stat_mod.S_ISDIR(os.lstat(os.path.dirname(text)).st_mode):
                return "", board.MANUAL_CHECK_PLACE_REFUSAL
        except OSError:
            return "", board.MANUAL_CHECK_PLACE_REFUSAL
        resolved = BoardVerbsMixin._canonical_path(text)
        if not BoardVerbsMixin._manual_check_home(resolved):
            return "", board.MANUAL_CHECK_PLACE_REFUSAL
        problems = manual_check.check(manual_check.read_text(resolved))
        if problems:
            return "", (board.MANUAL_CHECK_MALFORMED_REFUSAL
                        + manual_check.brief(problems))
        return resolved, ""

    def _manual_checks_sync(self, root: str = "", query: str = "",
                            status: str = "") -> dict:
        """The Checks section's page. **Blocking** — executor only.

        `root` empty means every enrolled root (the person's list across
        their projects — nothing in a check is a secret the board does not
        already show); a named root must be enrolled, refused in
        `_knowledge_enrolled_root`'s words. Per root `manual_check.scan`,
        then the status filter and `manual_check.matches`, each row joined
        to a card through `by_manual_check_path`; ordered open first, then
        newest first."""
        if root:
            try:
                roots = [self._knowledge_enrolled_root(root)]
            except ValueError as exc:
                return {"supported": True, "available": False,
                        "root": str(root), "checks": [],
                        "truncated": False, "reason": str(exc)}
        else:
            roots = sorted(enrollment.enrolled_roots())
        wanted = str(status or "").strip().lower()
        if wanted == "all":
            wanted = ""
        checks = []
        truncated = False
        limit = manual_check.MAX_CHECKS_PER_ROOT
        for base in roots:
            found = manual_check.scan(base, limit + 1)
            if len(found) > limit:
                truncated = True
                found = found[:limit]
            label = enrollment.enrolled_label(base) or os.path.basename(base)
            # The scan's folder names are the disk's own; the root is put in
            # the disk's spelling once, so every listed path is the one the
            # flag stored and the press compares.
            spelled = BoardVerbsMixin._canonical_path(base)
            for entry in found:
                if wanted and entry.get("status", "") != wanted:
                    continue
                if not manual_check.matches(entry, query):
                    continue
                path = os.path.join(spelled, manual_check.FOLDER,
                                    entry.get("folder", ""),
                                    manual_check.CHECK_NAME)
                cards = self._board.by_manual_check_path(path) \
                    if self._board is not None else []
                checks.append({
                    "path": path,
                    "folder": entry.get("folder", ""),
                    "title": entry.get("title", ""),
                    "card": entry.get("card", ""),
                    "card_id": str(cards[0].get("id") or "") if cards else "",
                    "project": entry.get("project", "") or label,
                    "root": base,
                    "check": entry.get("check", ""),
                    "created": entry.get("created", ""),
                    "status": entry.get("status", ""),
                    "outcome": entry.get("outcome", ""),
                    "checked_at": entry.get("checked_at", ""),
                    "steps_preview": entry.get("steps_preview", ""),
                    "malformed": bool(entry.get("malformed")),
                    "problem": entry.get("problem", ""),
                })
        checks.sort(key=manual_check.sort_key, reverse=True)
        checks.sort(key=lambda e: 0 if e["status"] == "open" else 1)
        return {"supported": True, "available": True,
                "root": roots[0] if root else "", "checks": checks,
                "truncated": truncated, "reason": ""}

    async def manual_checks_report(self, root: str = "", query: str = "",
                                   status: str = "") -> dict:
        """Every manual check under the enrolled roots (or one), open first
        then newest first. One `run_in_executor` hop: the scans, the reads
        and the store joins never run on the loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._manual_checks_sync, root, query, status)

    def _manual_check_text_sync(self, path: str) -> dict:
        resolved, refusal = self._manual_check_place(path)
        if refusal:
            return {"available": False, "path": str(path or ""),
                    "text": "", "reason": refusal}
        text = manual_check.read_text(resolved)
        if not text:
            return {"available": False, "path": resolved, "text": "",
                    "reason": "that check cannot be read"}
        return {"available": True, "path": resolved, "text": text,
                "reason": ""}

    async def manual_check_text(self, path: str) -> dict:
        """One check file's text, `_card_report`'s shape
        (`{available, path, text, reason}`), only for a file
        `_manual_check_place` admits now. One executor hop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._manual_check_text_sync, path)

    def _record_manual_outcome_sync(self, path: str, status: str,
                                    note: str) -> tuple:
        """`(resolved, header, refusal)`. **Blocking** — executor only.
        The place re-checked at the write, then `manual_check.write_outcome`,
        which re-reads the file and refuses anything but `Status: open`. The
        whole sequence holds `_MANUAL_OUTCOME_LOCK`, so of two concurrent
        presses exactly one writes. An already-recorded file comes back with
        its resolved path beside `MANUAL_OUTCOME_RECORDED_REFUSAL`, so the
        caller can still settle the cards flagged with it."""
        with _MANUAL_OUTCOME_LOCK:
            resolved, refusal = self._manual_check_place(path)
            if refusal:
                return "", {}, refusal
            header = manual_check.read_header(resolved)
            if header.get("status", "") != "open":
                return resolved, header, board.MANUAL_OUTCOME_RECORDED_REFUSAL
            refusal = manual_check.write_outcome(resolved, status, note)
            if refusal == manual_check.RECORDED:
                return resolved, header, board.MANUAL_OUTCOME_RECORDED_REFUSAL
            if refusal:
                return "", {}, refusal
            return resolved, header, ""

    async def _clear_cards_for_check(self, resolved: str) -> list:
        """Clear `manual_steps` on every card flagged with this check file
        that still carries steps; one publish for the lot, then each card's
        Needs you entry settled. The cards cleared, as the store returned
        them."""
        cleared = []
        if self._board is None:
            return cleared
        cards = await self._board_call("by_manual_check_path", resolved) or []
        for card in cards:
            if not str(card.get("manual_steps") or ""):
                continue
            after, _detail = await self._board_call("clear_manual", card["id"])
            if after is not None:
                cleared.append(after)
        if cleared:
            await self._publish_board()
            for card in cleared:
                await self._settle_handled_card(
                    str(card.get("id") or ""),
                    str(card.get("closed_by") or card.get("session_id") or ""))
        return cleared

    async def record_manual_outcome(self, path: str, status: str,
                                    note: str = "") -> tuple:
        """A person recording Passed or Failed on a check file. `(ok,
        detail)`.

        Dark Army is the file's **single writer**: the three status lines and
        nothing else (`manual_check.write_outcome`). The guard is current
        state re-checked at the write — the place, the shape and `Status:
        open` — so a second press is `MANUAL_OUTCOME_RECORDED_REFUSAL` and
        writes nothing. Then every card whose flag named this file has its
        steps cleared through the store's own `clear_manual`, so the badge,
        the inbox, the lifecycle episode and `_settle_checked_card` follow by
        the seams they already have; one `_publish_board()` for the lot. A
        Failed moves no card — Reopen is the person's."""
        wanted = str(status or "").strip().lower()
        if wanted not in manual_check.OUTCOMES:
            return False, manual_check.BAD_OUTCOME
        text = " ".join(str(note or "").split())[:board.MAX_MANUAL_OUTCOME_CHARS]
        loop = asyncio.get_running_loop()
        resolved, header, refusal = await loop.run_in_executor(
            None, self._record_manual_outcome_sync, path, wanted, text)
        if refusal == board.MANUAL_OUTCOME_RECORDED_REFUSAL and resolved:
            # The file already carries an outcome — written by an earlier
            # press, another app, or a person editing it. The refusal words
            # stand, but a card still wearing the badge for that file is
            # settled, or it would keep it for ever with Mark checked hidden.
            settled = await self._clear_cards_for_check(resolved)
            for card in settled:
                self._log_card_event(card, "card_manual_clear")
            return False, refusal
        if refusal:
            return False, refusal
        cleared = await self._clear_cards_for_check(resolved)
        for card in cleared:
            self._log_card_event(card, "card_manual_outcome",
                                 status=wanted, note=text[:200])
        if not cleared:
            self._log_event(
                "card_manual_outcome",
                project=str(header.get("project") or ""),
                title=str(header.get("check") or header.get("card") or ""),
                detail={"status": wanted, "note": text[:200]})
        return True, "recorded " + wanted

    def _scout_report_sources(self, root: str = "") -> tuple:
        """``(roots, cards)`` for the scout-report reads. **Blocking** —
        the store's lock is taken here, on the executor thread. ``root``
        narrows to one enrolled project (refused in
        `_knowledge_enrolled_root`'s words); empty means every enrolled
        root."""
        if root:
            roots = [self._knowledge_enrolled_root(root)]
        else:
            roots = sorted(enrollment.enrolled_roots())
        cards = self._board.reports_index_rows() \
            if self._board is not None else []
        return roots, cards

    @staticmethod
    def _scout_label(root: str) -> str:
        return enrollment.enrolled_label(root) or os.path.basename(root)

    def _scout_reports_index_sync(self, root: str = "",
                                  query: str = "") -> dict:
        roots, cards = self._scout_report_sources(root)
        if query:
            return scout_index.search(
                roots, cards, query, self._scout_label,
                refusal=BoardVerbsMixin._plan_path_refusal)
        return scout_index.build(roots, cards, self._scout_label,
                                 refusal=BoardVerbsMixin._plan_path_refusal)

    def _scout_report_body_sync(self, path: str) -> dict:
        roots, cards = self._scout_report_sources()
        return scout_index.read(path, roots, cards, self._scout_label,
                                refusal=BoardVerbsMixin._plan_path_refusal)

    async def scout_reports_index(self, root: str = "",
                                  query: str = "") -> dict:
        """Every scout report under the enrolled roots plus every card's
        report, newest first, no bodies (`scout_index.build`) — or, with a
        ``query``, only the reports whose body holds it, each with a
        snippet (`scout_index.search`, which raises `ValueError` in words
        on a term out of bounds). One `run_in_executor` hop: the scan, the
        reads and the store read never run on the loop.

        **One text search at a time per daemon**: a worst-case search reads
        and folds up to 25 MB, and the Mac and the phone can both ask, so
        a second waits on `_scout_search_lock` (made on the loop, lazily)
        rather than taking another executor worker. The plain list is not
        held behind it."""
        loop = asyncio.get_running_loop()
        if not query:
            return await loop.run_in_executor(
                None, self._scout_reports_index_sync, root, query)
        lock = getattr(self, "_scout_search_lock", None)
        if lock is None:
            lock = self._scout_search_lock = asyncio.Lock()
        async with lock:
            return await loop.run_in_executor(
                None, self._scout_reports_index_sync, root, query)

    async def scout_report_body(self, path: str) -> dict:
        """One report's text split into answer block and body, only for a
        path in the closed set `scout_index.locate` re-checks now. One
        `run_in_executor` hop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._scout_report_body_sync, path)

    def _plan_sources(self, root: str = "") -> tuple:
        """``(roots, cards)`` for the plan reads — `_scout_report_sources`'
        rule over the cards' `plan_path`. **Blocking** — the store's lock
        is taken here, on the executor thread. ``root`` narrows to one
        enrolled project (refused in `_knowledge_enrolled_root`'s words);
        empty means every enrolled root."""
        if root:
            roots = [self._knowledge_enrolled_root(root)]
        else:
            roots = sorted(enrollment.enrolled_roots())
        cards = self._board.plans_index_rows() \
            if self._board is not None else []
        return roots, cards

    def _plans_index_sync(self, root: str = "") -> dict:
        roots, cards = self._plan_sources(root)
        return plan_index.build(roots, cards, self._scout_label,
                                refusal=BoardVerbsMixin._plan_path_refusal)

    def _plan_body_sync(self, path: str) -> dict:
        roots, cards = self._plan_sources()
        return plan_index.read(path, roots, cards, self._scout_label,
                               refusal=BoardVerbsMixin._plan_path_refusal)

    async def plans_index(self, root: str = "") -> dict:
        """Every dated plan under the enrolled roots plus every card's
        plan, newest first, no bodies (`plan_index.build`). One
        `run_in_executor` hop: the scan, the head reads and the store read
        never run on the loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._plans_index_sync, root)

    async def plan_body(self, path: str) -> dict:
        """One plan's text, only for a path in the closed set
        `plan_index.locate` re-checks now. One `run_in_executor` hop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._plan_body_sync, path)

    def _knowledge_person_sync(self, method: str, root: str, key: str,
                               *rest):
        normalised = self._knowledge_enrolled_root(root)
        if self._board is None:
            return False, _D.KNOWLEDGE_STORE_REFUSAL
        return getattr(self._board, method)(normalised, key, *rest)

    async def knowledge_confirm(self, root: str, key: str) -> tuple:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._knowledge_person_sync, "knowledge_confirm",
            root, key)

    async def knowledge_mark_stale(self, root: str, key: str) -> tuple:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._knowledge_person_sync, "knowledge_mark_stale",
            root, key)

    async def knowledge_edit(self, root: str, key: str, question: str,
                             answer: str) -> tuple:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._knowledge_person_sync, "knowledge_edit",
            root, key, question, answer)

    async def _handle_board_close_request(self, msg: dict) -> dict:
        """A session declaring, through Dark Army's channel, that the card it is
        working on is finished.

        **The hook socket authenticates nothing** — `_attach_is_displaced` says
        so in its own docstring — so as with `_handle_board_card_request` the
        port, pid and session id here are merely *claimed*.

        What made an unauthenticated **create** tolerable was stated there: the
        worst a forged message achieves is a card a human has to read and then
        drag across the board. A **close** spends exactly that tolerance, and it
        is bought back by **scope**:

        * The message carries **no card id**, and the tool's schema has no
          property for one. The card is found solely by asking which session is
          on the other end of `port` (`_channel_session`, by pid at the moment
          of use) and looking up the cards bound to that session id.
        * **The port is guessable, and the scope — not the guessing — is the
          defence.** `port` is a plain integer in a JSON line on the hook
          socket: reaching one costs enumerating 64k values against
          `127.0.0.1`, not winning the attach race and not connecting to the
          channel at all. Say it that way round, because the opposite framing
          ("an attacker would have to win a race") is how a later widening
          talks itself into taking a card id. What actually bounds this is that
          a forger can only close the card that session is *already executing*
          — work it is already the recorded author of, on a board already
          showing it as in progress. That gains an attacker nothing they did
          not already have, and it is the entire reason this verb is
          acceptable.
        * Every other shape is refused outright: no attribution, no card, more
          than one card, or a card already in Done.

        **Anyone loosening this to take a card id has deleted the
        justification**, not merely widened an API: it would turn a socket
        anything on the machine can write to into a way to mark arbitrary work
        finished.

        This is also not Dark Army deciding anything. `_reconcile_board`'s rule stands
        untouched — a session ending still moves no card — and what is recorded
        here is a *statement*, stored with the name of whoever made it.

        **It mints no `_board_author_guard`, and that is the one deliberate
        difference from the two verbs beside it.** `dark_army_add_card` and
        `dark_army_attach_plan` both re-read the registry after their awaits — the
        card they write is composed from what the channel is still saying — so
        a channel displaced mid-hop would have them write on stale authority.
        This verb reads nothing after the resolution: the only thing that
        crosses the two executor hops is the resolved `session_id`, and
        `declare_done`'s own WHERE clause pins both that id and the column the
        card was observed in, so a displaced channel cannot redirect the write.
        The residual is a close attributed to a session whose exact-holder
        proof decayed inside those few milliseconds, which is the same residual
        the guard itself leaves.
        """
        port = int(msg.get("port") or 0)
        # Fresh native-holder attribution, `dark_army_add_card`'s and
        # `dark_army_attach_plan`'s: this verb is open to Codex, and a card moving
        # to Done on the weaker cwd fallback would be a stronger claim made
        # on thinner evidence than the two verbs beside it. For Claude and
        # Grok this is the announced id, exactly as before.
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.close_card_by_session(
            session_id, str(msg.get("note") or ""))
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s closed board card %s", session_id[:12],
                    card["id"][:8])
        return {"ok": True, "detail": "moved to Done",
                "card_id": card["id"], "column": card["column_name"],
                "title": card["title"]}

    async def _handle_board_manual_request(self, msg: dict) -> dict:
        """A session declaring, through Dark Army's channel, that its card needs a
        hand-check before anybody calls it finished.

        `_handle_board_close_request`'s scope discipline, and it buys back less
        because it spends less:

        * The message carries **no card id**, and the tool's schema has no
          property for one. The card is found solely by asking which session is
          on the other end of `port` (`_channel_session`, by pid at the moment
          of use) and looking up the cards bound to that session id.
        * The port is guessable, and the **scope** is the defence, not the
          guessing. What a forger who reaches it achieves is a badge and some
          steps on a card that session is already executing — nothing moves,
          nothing starts, and a person clears it with one press. Strictly less
          than the close verb beside it already tolerates.
        * Every other shape is refused outright: no attribution, no card, more
          than one open card, or a card already in Done.

        **Anyone loosening this to take a card id has deleted the
        justification**, not widened an API — the same sentence stands here as
        on the close verb, for the same reason.
        """
        port = int(msg.get("port") or 0)
        # Fresh native-holder attribution, the close verb's: this verb is open
        # to Codex too, and a flag is what releases a card's place in the
        # queue. For Claude this is the announced id, exactly as before.
        session_id = (await self._board_request_session_fresh(port) or "") if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.flag_manual_by_session(
            session_id, str(msg.get("steps") or ""),
            str(msg.get("path") or "")[:1024])
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s flagged a manual check on board card %s",
                    session_id[:12], card["id"][:8])
        return {"ok": True, "detail": "manual check flagged",
                "card_id": card["id"], "column": card["column_name"],
                "title": card["title"]}

    async def _handle_board_answer_request(self, msg: dict) -> dict:
        """A session answering, through Dark Army's channel, a question about its card.

        **The hook socket authenticates nothing** — same as
        `_handle_board_card_request`. The message carries **no card id**; the
        card is resolved from the calling session (`_channel_session`) and the
        consult / `by_session` ladder. Unattributable is refused outright.
        """
        port = int(msg.get("port") or 0)
        session_id = self._board_request_session(port) or "" if port else ""
        if not session_id:
            return {"ok": False, "detail": "Dark Army could not tell which session asked"}
        if self._board is None:
            return {"ok": False, "detail": "the board is not open"}
        card, detail = await self.answer_card_by_session(
            session_id, str(msg.get("answer") or ""))
        if card is None:
            return {"ok": False, "detail": detail}
        logger.info("session %s answered board card %s", session_id[:12],
                    card["id"][:8])
        return {"ok": True, "detail": "answered",
                "card_id": card["id"], "title": card.get("title") or ""}

    # --- Suggesting how important a card is (see card_priority.py) ---

    def _consider_priority(self, card: dict) -> None:
        """Queue a card for one importance score, at most once, from the executor.

        `_consider_title`'s shape exactly: runs inside `_reconcile_board` and
        spends nothing — no subprocess, no fork. Skips, each with its reason:

        - a card whose stored `priority` is already non-empty — a person's
          number, or Dark Army's own from an earlier pass, and there is one attempt
          ever. That skip **records the card in the shop on its way out**,
          rather than returning above it: without that, a number somebody
          typed by hand is never learned, and clearing it later would put the
          card straight back in front of the helper — Dark Army re-scoring a card
          somebody deliberately emptied, which is the opposite of what
          clearing means. Written as a *hit*, so it persists to
          `card-priority.json` and survives a restart;
        - a card in `done` — `_consider_title`'s argument (paying a
          subprocess to score a finished record), and one more that is
          load-bearing: `update()` re-stamps `updated_at`, and `done_since` /
          `done_awaiting_review` order on `COALESCE(done_at, updated_at)`, so
          scoring a Done card would silently reorder the Recently-finished
          preview and the review banner;
        - a card with neither title nor summary.

        Everything else is scored, a refining Prep card and an `in_progress`
        card included. That covers **every** surface by construction: the
        panel composer, the phone composer and `dark_army_add_card` all end as a row
        in `board.db`, and the reconcile reads rows.
        """
        card = card or {}
        cid = str(card.get("id") or "")
        stored = str(card.get("priority") or "").strip()
        if stored:
            # `learn`, not `consider`: this card has an answer, whoever
            # wrote it, and the shop has to hold it durably or an emptied
            # card is asked about again on the next pass. Idempotent, so the
            # reconcile does not rewrite the file every few seconds.
            self._priority_shop.learn(cid, stored)
            return
        if str(card.get("column_name") or "") == "done":
            return
        title = str(card.get("title") or "").strip()
        summary = str(card.get("summary") or "").strip()
        if not title and not summary:
            return
        if self._priority_shop.consider(cid) == card_priority.ASK:
            self._priority_queue.append(
                (cid, title, summary, str(card.get("project") or "")))

    async def _flush_card_priorities(self) -> None:
        """Score the cards the reconcile just asked about, one at a time.

        Serial and detached (`self._priority_task`), for
        `_flush_session_titles`' stated reason: this is a background nicety on
        the agents-push path, a ten-second helper awaited there would hold the
        panel a snapshot behind the fleet, and a fresh board of thirty cards
        must drain at one score per snapshot cycle rather than fork thirty
        Claude processes at once.
        """
        if not self._priority_queue:
            return
        if self._priority_task is not None and not self._priority_task.done():
            return
        cid, title, summary, project = self._priority_queue.pop(0)
        self._priority_task = asyncio.ensure_future(
            self._score_card(cid, title, summary, project))

    async def _score_card(self, card_id: str, title: str, summary: str,
                          project: str) -> None:
        """One `claude -p`, and whichever number it names for this card.

        Every failure ends the same way — the shop remembers the miss, the
        card stays unscored, nothing is retried and **nothing writes
        `dispatch_error`**: unscored is a legitimate resting state, not a
        refusal, and the orange line is for a launch that was asked for and
        declined.
        """
        claude_bin = agents_poll.find_claude_binary()
        if not claude_bin:
            self._priority_shop.note_failed(card_id)
            return
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *card_priority.argv(claude_bin, title=title, summary=summary,
                                    project=project),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # The state dir, the filer's cwd and **not**
                # `card_prepare`'s: a scorer reads a title and a summary, and
                # has no business loading somebody's CLAUDE.md.
                cwd=str(_D.STATE_DIR),
                env=subprocess_env.clean_env(),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=card_priority.TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, OSError, ValueError):
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            self._priority_shop.note_failed(card_id)
            logger.debug("card priority for %s did not finish", card_id[:8],
                         exc_info=True)
            return
        if proc.returncode:
            # `card_prepare`'s check, and here it buys more: stderr is DEVNULL'd, so a
            # failing CLI's short stdout line is the only leak, and there is
            # one attempt ever — nothing would ever correct a miss stored as
            # a hit.
            self._priority_shop.note_failed(card_id)
            logger.debug("card priority for %s exited %d", card_id[:8],
                         proc.returncode)
            return
        score = self._priority_shop.note(
            card_id, out.decode("utf-8", "replace"))
        if not score:
            logger.debug("card priority for %s was not a number", card_id[:8])
            return
        # **Re-read before writing, and give way to a number that is already
        # there.** The helper takes ten to forty-five seconds, which is
        # exactly long enough for somebody to open the brand-new card and
        # type a number of their own — and `update` would simply overwrite
        # it, silently, moving the card in its column. The card was unscored
        # when the reconcile queued it; the only thing that decides is what
        # the row says *now*.
        #
        # A re-read rather than an `expected_revision` guard: the revision
        # this task holds is from before the subprocess ran, so guarding on
        # it would refuse every card whose reconcile had written anything at
        # all in between (a bind, a queue state), and Dark Army would stop scoring.
        # Board calls are serialised on the executor, so the window between
        # this read and the write below is microseconds rather than the
        # helper's whole run.
        existing = await self._board_call("get", card_id)
        if not isinstance(existing, dict):
            logger.debug("card %s went while it was being scored", card_id[:8])
            return
        if str(existing.get("priority") or "").strip():
            # A person got there first. Their number stands, and the shop
            # already holds Dark Army's answer, so nothing is retried.
            logger.debug("card %s was scored by hand while Dark Army was asking",
                         card_id[:8])
            return
        # The ordinary `update`, with no `bump=False`: the score is written
        # once per card ever and is a change to something a person reads, so
        # a phone holding a stale copy has to be told to re-fetch it.
        card, detail = await self._board_call(
            "update", card_id, {"priority": score})
        if card is not None:
            logger.info("Scored card %s at %s", card_id[:8], score)
        else:
            # The card went between the read above and this write. A debug
            # line, never an orange one.
            logger.debug("card %s was not scored: %s", card_id[:8], detail)

    # --- What Dark Army observed a run do (`work_record.py`) -------------------
    #
    # The decide-on-the-executor / flush-on-the-loop split this file already
    # uses three times (`_consider_priority`, `_consider_title`,
    # `_decide_auto_compacts`). The reconcile spends nothing; the loop runs
    # the subprocesses, serially and detached, one card per snapshot cycle.
    #
    # The daemon reaches the run table only through the store's verbs — its
    # name does not appear in this file, and that is a grep criterion: a
    # second writer is how a record a person is asked to trust stops being
    # one.

    # --- Starting the work by itself once the plan lands ------------------

    def _schedule_auto_start(self, card: dict) -> None:
        """Act on a card's standing `start_when_planned` instruction.

        `_schedule_work_baseline`'s shape exactly, for the same reason and
        one more of its own. **Scheduled, deliberately not awaited inline**:
        the caller is `attach_plan_by_session`, whose reply travels back down
        a `channel_server` socket with a five-second call timeout that has
        already been partly spent resolving the session. Awaiting a
        `dispatch.spawn` round-trip to the VS Code extension on that reply
        would time the tool call out and tell the refining agent its plan had
        **not** attached, when it had. The attach's reply is always the
        attach's own; the auto-start reports itself on the card.
        """
        if not isinstance(card, dict):
            return
        card_id = str(card.get("id") or "")
        if not card_id or str(card.get("start_when_planned") or "") != "1":
            return
        try:
            task = asyncio.ensure_future(self._auto_start_after_refine(card_id))
        except RuntimeError:
            # No running loop (a synchronous test calling the attach path).
            return
        self._auto_start_tasks.add(task)
        task.add_done_callback(self._auto_start_tasks.discard)

    async def _auto_start_after_refine(self, card_id: str) -> None:
        """Start a freshly planned card as if a person had pressed Start.

        **`dispatch_card` and nothing else** — no `allow_unplanned`, no
        `skip_plan_gate`, no `queued_replay`. This is one press' worth of
        capability and not a new one: the plan gate, the enrolment refusal,
        `dispatch.guard` and the slot gate all run, a
        full project queues exactly as a press does, and a hard refusal lands
        as the orange `dispatch_error` line the board already draws.
        """
        try:
            # Spend the tick on the *attempt*, before it: a card sent back and
            # planned again has to be ticked again on purpose, and a refusal
            # must not leave a standing instruction that fires on the next
            # attach. `bump=False` because Dark Army acting on a standing
            # instruction is not somebody editing the card — a revision that
            # moved here would refuse the save of whoever had it open, which
            # is verbatim the failure `REVISED_COLUMNS`' docstring avoids.
            # No `_publish_board()`: every branch below publishes a moment
            # later, and an extra frame per attach is churn against the
            # broadcast limiter.
            await self._board_call("update", card_id,
                                   {"start_when_planned": ""}, bump=False)
            ok, detail = await self.dispatch_card(card_id)
            if ok:
                # `_dispatch_card_locked` has already published and logged
                # `card_dispatched`.
                return
            card = await self._board_call("get", card_id)
            if card is None:
                return
            if str(card.get("queue_state") or "") == "queued":
                # The project was full. The enqueue has already written the
                # slot and published, and `queue_reason` is composed onto the
                # card per frame — the same sentence a press would have got.
                # Classified by re-reading `queue_state`, never by matching
                # the prose: `start_project`'s stated rule.
                return
            # A refusal that will still refuse in an hour, with nobody looking
            # at the press that caused it. Written here rather than by
            # widening `_queue_hard_refusal`, which would start writing orange
            # lines onto every hand press that bounces. The card is left in
            # `backlog` where `attach_plan` put it; nothing moves it.
            await self._board_call("update", card_id,
                                   {"dispatch_error": detail}, bump=False)
            await self._publish_board()
            self._log_card_event(card, "card_dispatch_failed", error=detail)
        except Exception:
            logger.warning("auto-start after refine failed for card %s",
                           card_id[:8], exc_info=True)

    def _schedule_work_baseline(self, card_id: str, root: str,
                                run_at: float) -> None:
        """Note where the project stands, without making the press wait.

        Tracked in `_work_open_tasks` with a done-callback that discards, so
        a task neither leaks nor is garbage-collected mid-flight; `run()`'s
        shutdown does not have to know about it because every path through
        `_open_work_record` is bounded.
        """
        if not card_id or not root:
            return
        try:
            task = asyncio.ensure_future(
                self._open_work_record(card_id, root, run_at))
        except RuntimeError:
            # No running loop (a synchronous test calling the dispatch path).
            # A missing baseline is a stated, recoverable state.
            return
        self._work_open_tasks.add(task)
        task.add_done_callback(self._work_open_tasks.discard)

    async def _open_work_record(self, card_id: str, root: str,
                                run_at: float) -> None:
        """One bounded `git rev-parse HEAD`, then one `open_run`.

        Every failure — no git, no repository, a timeout, a non-zero exit —
        is one debug line and **no row**. The run is still recorded when it
        ends; it simply says it had no starting point, which is a fact worth
        stating rather than a reason to keep no record at all.
        """
        ok, out, _ = await self._run_git(work_record.argv_baseline(root), root)
        baseline = out.decode("utf-8", "replace").strip() if ok else ""
        if not baseline:
            logger.debug("no baseline for card %s in %s", card_id[:8], root)
            return
        await self._board_call("open_run", card_id, float(run_at), root,
                               baseline)

    async def _run_git(self, argv: list, root: str, *,
                       truncate: bool = False,
                       timeout: Optional[float] = None,
                       env: Optional[dict] = None) -> tuple:
        """`(ok, output, reason)` for one bounded git call. Never raises.

        ``truncate`` decides what an over-long reading means. For the two
        listing calls it is False and over-long is a **refusal**: half a
        `--numstat` is a file list that silently omits files, which is worse
        than saying it could not be read. For one file's diff it is True —
        a cut is what `MAX_DIFF_BYTES` already does one rung up, and it is
        stated to the reader rather than inferred.

        Bounded three ways, all of them mandatory (`work_record`'s docstring
        says why): a timeout, a cap on what is read off the pipe, and an
        environment that cannot prompt. `reason` is one of `work_record`'s
        own sentences, so the words a person reads are decided in one place.

        **`subprocess.run` on the executor, not `create_subprocess_exec`**,
        and the reason is the baseline reading rather than taste. That one is
        deliberately detached and never awaited (a slow git must not delay a
        terminal that has already opened), and an asyncio subprocess
        transport left in flight when its loop closes wedges the close — a
        hang, not an error, reproduced by one dispatch under pytest. The
        executor hop is what every blocking thing in this file already does
        (`_board_call`'s argument), it keeps the loop free exactly the same
        way, and `subprocess.run(timeout=…)` is the same bound.

        ``timeout`` widens the bound for the two worktree calls that write a
        tree or talk to a remote (`worktrees.WORKTREE_ADD_TIMEOUT_SECONDS`,
        `FETCH_TIMEOUT_SECONDS`); absent is `GIT_TIMEOUT_SECONDS`, as always.

        ``env`` replaces `work_record.git_env()` for the one caller that
        needs more of it: the merge (`merges.merge_env`, which keeps git
        from ever opening an editor).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(
            self._git_blocking, argv, root, truncate=truncate,
            timeout=timeout, env=env))

    def _git_blocking(self, argv: list, root: str, *, truncate: bool = False,
                      timeout: Optional[float] = None,
                      limit: Optional[int] = None,
                      env: Optional[dict] = None) -> tuple:
        """`_run_git`'s runner: the same bounded call, for code that is
        already on the executor (the setup script's last look, which runs
        immediately before the script). Every git call still goes through
        this one body. Blocking; never on the loop.

        ``limit`` widens the output cap for the one listing that needs it
        (`worktrees.MAX_INDEX_DOTFILES_BYTES`); absent is
        `work_record.MAX_GIT_OUTPUT_BYTES`."""
        limit = int(limit or work_record.MAX_GIT_OUTPUT_BYTES)
        bound = float(timeout or work_record.GIT_TIMEOUT_SECONDS)
        environment = env if env is not None else work_record.git_env()

        def call() -> tuple:
            try:
                done = subprocess.run(
                    list(argv), cwd=str(root), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=environment,
                    timeout=bound)
            except subprocess.TimeoutExpired:
                logger.debug("git %s timed out in %s", argv[-2:], root)
                return False, b"", work_record.GIT_TIMEOUT_REASON
            except (OSError, ValueError):
                logger.debug("git could not run in %s", root, exc_info=True)
                return False, b"", work_record.GIT_FAILED_REASON
            out = done.stdout or b""
            if len(out) > limit:
                if not truncate:
                    return False, b"", work_record.TOO_MUCH_REASON
                out = out[:limit]
            if done.returncode:
                # The output rides back with the failure on purpose: `git diff
                # --no-index` exits 1 precisely *because* the two sides differ,
                # which for an untracked file is the whole point of the call.
                # The caller decides what a non-zero exit with real output
                # means.
                return False, out, work_record.GIT_FAILED_REASON
            return True, out, ""

        return call()

    @staticmethod
    def _snapshot_row_in(snapshot: dict, session_id: str):
        """That session's row in *this* snapshot, or None. The reconcile is
        handed the snapshot every other surface reads, and reading it here
        rather than `_agents_snapshot_cache` keeps one pass consistent with
        itself."""
        if not session_id or not isinstance(snapshot, dict):
            return None
        for group in snapshot.values():
            if not isinstance(group, list):
                continue
            for row in group:
                if isinstance(row, dict) and row.get("session_id") == session_id:
                    return row
        return None

    def _consider_work_record(self, card: dict, snapshot: dict) -> None:
        """Queue one ended run for its record. **Executor, no I/O.**

        Reads the closing words off the snapshot row the rail already draws
        (`last_text` / `last_summary`, populated for all three providers) —
        the `## Work done` report lands there whole; the record stores the
        raw `last_report`, and the parsed shape rides the row alone
        (`work_report.py`).

        Idempotent twice over: `mark_ended` writes `ended` exactly once, but
        a restart re-runs the grace, so a card whose stored record already
        names this run is skipped.
        """
        card = card if isinstance(card, dict) else {}
        cid = str(card.get("id") or "")
        sid = str(card.get("session_id") or "")
        # The run's folder: the card's own worktree where it worked in one
        # (`docs/card-worktrees.md`), the project root otherwise.
        root = str(card.get("worktree_path") or card.get("root") or "")
        if not cid or not sid or not root:
            return
        try:
            run_at = float(card.get("dispatched_at") or 0.0)
        except (TypeError, ValueError):
            run_at = 0.0
        if not run_at:
            return
        if self._work_recorded.get(cid) == run_at:
            return
        if any(item[0] == cid for item in self._work_record_queue):
            return
        if self._board is not None:
            try:
                existing = self._board.run_for(cid)
            except Exception:
                existing = None
            if isinstance(existing, dict) and existing.get("recorded_at") \
                    and float(existing.get("run_at") or 0.0) == run_at:
                self._work_recorded[cid] = run_at
                return
        row = self._snapshot_row_in(snapshot, sid) or {}
        self._work_record_queue.append((
            cid, sid, root, run_at,
            work_record.verdict_for(card, sid),
            str(row.get("last_report") or row.get("last_text") or ""),
            str(row.get("last_summary") or ""),
        ))

    async def _collect_work_record_now(self, card_id: str) -> None:
        """Collect this one card's queued record **now**, awaited, rather
        than whenever `_flush_work_records` reaches it.

        The batch-implement advance's reason: the record's file list is a
        diff from the card's baseline to the working tree *at collection*,
        and the next card of the batch starts editing the same tree the
        moment it is bound. Collected before that bind, card k's list holds
        card k's work and not card k+1's. No queued item (already recorded,
        or already being collected by the flush) is a no-op."""
        for item in list(self._work_record_queue):
            if item[0] == card_id:
                try:
                    self._work_record_queue.remove(item)
                except ValueError:
                    return
                await self._collect_work_record(*item)
                return

    async def _flush_work_records(self) -> None:
        """Collect the record the reconcile just asked for, one at a time.

        `_flush_card_priorities`' shape verbatim, for its reason: this runs on
        the agents-push path, it costs two subprocesses, and a board of
        thirty ended cards must drain one per snapshot cycle rather than
        fork sixty git processes at once.

        The worktree releases the reconcile queued are started first, as one
        detached task (`_kick_worktree_releases` only schedules it) — the
        release itself is never awaited here, since a `git worktree remove`
        may take a minute. Each release waits for its
        own card's record, so it never removes a folder a collection is
        about to read.
        """
        try:
            await self._kick_worktree_releases()
        except Exception:
            logger.warning("worktree release failed to start", exc_info=True)
        if not self._work_record_queue:
            return
        if self._work_record_task is not None \
                and not self._work_record_task.done():
            return
        item = self._work_record_queue.pop(0)
        self._work_record_task = asyncio.ensure_future(
            self._collect_work_record(*item))

    async def _collect_work_record(self, card_id: str, session_id: str,
                                   root: str, run_at: float, verdict: str,
                                   report: str, summary: str) -> None:
        """Read what changed, and write the record. **Never skips it.**

        Every failure path here still calls `close_run`, with
        `files_available` false and a reason in `work_record`'s own words: a
        run that ended without a record is exactly the hole this whole
        feature exists to fill, and "Dark Army could not read the folder" is a
        fact a person can act on where silence is not.

        `rowcount == 0` inside `close_run` is the interesting case, not an
        error — the card was started again while this was out, and the newer
        run owns the row. The record is dropped and one debug line says so.
        """
        self._work_recorded[card_id] = run_at
        stored = await self._board_call("run_for", card_id)
        baseline = str((stored or {}).get("baseline") or "")
        files: list = []
        total = 0
        available = False
        reason = ""
        loop = asyncio.get_running_loop()
        enrolled = await loop.run_in_executor(
            None, enrollment.root_enrolled, root)
        if not root:
            reason = work_record.NO_ROOT_REASON
        elif not enrolled:
            reason = work_record.NOT_ENROLLED_REASON
        elif not baseline:
            reason = work_record.NO_BASELINE_REASON
        else:
            ok, out, why = await self._run_git(
                work_record.argv_numstat(root, baseline), root)
            if not ok:
                reason = why or work_record.GIT_FAILED_REASON
            else:
                rows = work_record.parse_numstat(
                    out.decode("utf-8", "replace"))
                ok2, others, why2 = await self._run_git(
                    work_record.argv_untracked(root), root)
                if ok2:
                    rows.extend(work_record.parse_untracked(others))
                files, _cut, total = work_record.clamp_files(rows)
                available = True
                # A partial reading is stated rather than published as a
                # whole one: the tracked half is real, the untracked half is
                # missing, and a reader has to be able to tell.
                reason = "" if ok2 else (why2 or work_record.GIT_FAILED_REASON)
        # What the shunt skill's helper did for this session, off the
        # ledger its wrappers appended (`paths.SHUNT_LEDGER_DIR`). The file
        # name is `work_record.ledger_name`, the wrappers' own character
        # map, so the read is under the name they wrote under and never
        # outside the directory; an id with no safe name reads nothing. A
        # file read, so on the executor beside the enrolment check; a
        # missing or unreadable ledger is zero delegations and an unknown
        # cost, never an error and never a skipped record.
        ledger = work_record.ledger_name(session_id)
        if ledger is None:
            delegations, kept_out, worker_cost = 0, 0, None
        else:
            delegations, kept_out, worker_cost = await loop.run_in_executor(
                None, work_record.read_shunt_ledger,
                paths.SHUNT_LEDGER_DIR / ledger)
        record, detail = await self._board_call(
            "close_run", card_id, float(run_at), session_id, verdict,
            report, summary, files, available, reason, total,
            shunt_delegations=delegations, shunt_lines_kept_out=kept_out,
            shunt_worker_cost_usd=worker_cost) or (None, "")
        if record is None:
            logger.debug("no work record for card %s: %s", card_id[:8], detail)
            return
        card = await self._board_call("get", card_id)
        logger.info("recorded what card %s changed: %d files, +%d -%d, "
                    "%d delegations",
                    card_id[:8], int(record.get("files_total") or 0),
                    int(record.get("lines_added") or 0),
                    int(record.get("lines_removed") or 0),
                    int(record.get("shunt_delegations") or 0))
        self._log_card_event(
            card or {"id": card_id}, "card_work_recorded",
            session_id=session_id, verdict=verdict,
            files=int(record.get("files_total") or 0),
            added=int(record.get("lines_added") or 0),
            removed=int(record.get("lines_removed") or 0),
            delegations=int(record.get("shunt_delegations") or 0))
        await self._publish_board()

    # --- Card worktrees (docs/card-worktrees.md) ---
    #
    # The prepare task and the release are the only places the daemon writes
    # a tree. Every git call runs through `_run_git` on argv `worktrees.py`
    # builds; the setup script and the trust copies run on the executor.

    def _wants_worktree(self, root) -> bool:
        """Whether a Start in `root` works in its own worktree: isolation on
        for the project and the root a git checkout. Blocking; executor."""
        return bool(root) and self._isolation_for(root) \
            and self._git_checkout(root)

    async def _reusable_worktree(self, head: dict) -> str:
        """The worktree recorded on `head`, when it is still one: under the
        root's `.worktrees/`, a directory, and named by `git worktree list`.
        `""` otherwise — a reset or a second Start reuses the folder and its
        branch, and a folder removed by hand is prepared afresh."""
        path = str((head or {}).get("worktree_path") or "")
        root = dispatch.normalise_root(str((head or {}).get("root") or ""))
        if not path or not root:
            return ""
        loop = asyncio.get_running_loop()
        usable = await loop.run_in_executor(
            None, lambda: worktrees.inside(root, path) and os.path.isdir(path))
        if not usable:
            return ""
        ok, out, _why = await self._run_git(
            worktrees.argv_worktree_list(root), root)
        if not ok:
            return ""
        listed = worktrees.parse_worktree_list(out.decode("utf-8", "replace"))
        if os.path.realpath(path) not in listed:
            return ""
        await self._restore_detached_folder(head, path)
        return path

    async def _restore_detached_folder(self, head: dict, path: str) -> None:
        """A card folder found detached and clean (a merge that a restart cut
        short) is put back on its recorded branch and its carried pack files
        restored, so a Start or a Fix never opens on a bare commit. A folder
        a merge is working in, or one with anything uncommitted, is left."""
        cid = str((head or {}).get("id") or "")
        branch = str((head or {}).get("worktree_branch") or "")
        if not branch or cid in (getattr(self, "_merging", None) or {}):
            return
        ok, _o, _w = await self._run_git(merges.argv_head_branch(path), path)
        if ok:
            return  # on a branch already
        if await self._merge_markers(path, merges.MID_MERGE_MARKERS):
            return  # a rebase or merge is under way: not ours to disturb
        ok, status, _w = await self._run_git(merges.argv_status(path), path)
        if ok and not status:
            await self._merge_back_to_branch(path, branch)

    def _start_worktree_prepare(self, head: dict, root: str,
                                replay: dict) -> None:
        """Mark `head` as preparing and start the task. Loop only."""
        cid = str(head.get("id") or "")
        preparing = dict(getattr(self, "_worktree_preparing", None) or {})
        # The token is the entry's own: a task pops only the entry it made,
        # never one a later preparation of the same card put there.
        token = secrets.token_hex(8)
        preparing[cid] = {"project": str(head.get("project") or ""),
                          "root": str(root), "since": time.time(),
                          "token": token}
        self._worktree_preparing = preparing
        tasks = getattr(self, "_worktree_tasks", None)
        if tasks is None:
            tasks = set()
            self._worktree_tasks = tasks
        task = asyncio.ensure_future(
            self._prepare_worktree_then_dispatch(dict(head), str(root),
                                                 dict(replay), token=token))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _pop_preparing(self, card_id: str, token: str = "") -> None:
        """Replace, never mutate: the executor may be reading the map. With a
        `token`, only the entry carrying it goes."""
        current = getattr(self, "_worktree_preparing", None) or {}
        entry = current.get(card_id)
        if token and (entry or {}).get("token") != token:
            return
        if card_id in current:
            self._worktree_preparing = {
                k: v for k, v in current.items() if k != card_id}

    async def _fail_prepare(self, card_id: str, error: str,
                            replay: dict) -> None:
        """Put a failed preparation's words on the card. A queued card is
        **dequeued in the same write** — `_queue_hard_refusal`'s rule: the
        drain would otherwise re-prepare it, and re-run the setup script,
        on every pass for ever."""
        fields: dict = {"dispatch_error": error}
        if replay.get("queued_replay"):
            fields.update({"queue_state": "", "queued_at": None})
        await self._board_call("update", card_id, fields, bump=False)
        card = await self._board_call("get", card_id)
        if card is not None:
            self._log_card_event(card, "card_dispatch_failed", error=error)

    async def _discard_worktree(self, root: str, path: str) -> None:
        """Remove a worktree nobody recorded (its card was deleted while it
        was being prepared) and take back its trust copies. **Never
        `--force`**: a refusal keeps the folder and is one log line. Only a
        directory under the root's own `.worktrees/`, which is not a link —
        the error branch may hand over a path that was never made."""
        loop = asyncio.get_running_loop()

        def ours() -> bool:
            folder = os.path.join(root, worktrees.WORKTREES_DIR)
            return (not os.path.islink(folder) and os.path.isdir(path)
                    and worktrees.inside(root, path))

        if not await loop.run_in_executor(None, ours):
            return
        removed, _o, why = await self._run_git(
            worktrees.argv_worktree_remove(root, path), root,
            timeout=worktrees.WORKTREE_ADD_TIMEOUT_SECONDS)
        await loop.run_in_executor(None, trust_marks.unmark, path)
        logger.info("discarded the unrecorded worktree %s: %s", path,
                    "removed" if removed else f"kept ({why})")

    async def _prepare_worktree_then_dispatch(self, head: dict, root: str,
                                              replay: dict, *,
                                              token: str = "") -> None:
        """Prepare the head card's worktree, record it, then finish the
        person's press. Loop; never raises.

        The re-entry is `dispatch_card` (or `start_cards` for a batch) with
        the press's own arguments — `_auto_start_after_refine`'s precedent:
        the completion of a press already made, running every gate again at
        this instant, never a new capability. The card is popped from
        `_worktree_preparing` **before** the re-entry, so the re-entry is
        not refused by its own preparation; a failure anywhere is one log
        line and the words on the card's `dispatch_error` (dequeuing a
        queued card, `_fail_prepare`), and `finally` always pops.
        """
        cid = str(head.get("id") or "")
        try:
            path, branch, error = await self._prepare_worktree(head, root)
            if error:
                logger.info("card %s: worktree not ready: %s", cid[:8], error)
                if await self._board_call("get", cid) is None:
                    # Deleted while this ran: nobody will record or release
                    # the folder, so it goes now (the refused-remove case
                    # keeps it, never `--force`).
                    await self._discard_worktree(
                        dispatch.normalise_root(root), path)
                    return
                await self._fail_prepare(cid, error, replay)
                return
            recorded, why = await self._board_call(
                "record_worktree", cid, path, branch) or (None, "")
            if recorded is None:
                logger.info("card %s: worktree ready but not recorded (%s)",
                            cid[:8], why)
                if await self._board_call("get", cid) is None:
                    await self._discard_worktree(
                        dispatch.normalise_root(root), path)
                return
            logger.info("card %s: worktree %s on %s is ready", cid[:8], path,
                        branch)
            self._pop_preparing(cid, token)
            batch_ids = replay.get("batch_ids")
            if batch_ids:
                ok, detail = await self.start_cards(list(batch_ids))
                target = cid
            else:
                target = str(replay.get("card_id") or cid)
                ok, detail = await self.dispatch_card(
                    target,
                    allow_unplanned=bool(replay.get("allow_unplanned")),
                    queued_replay=bool(replay.get("queued_replay")),
                    own_terminal=replay.get("own_terminal"))
            if ok:
                return
            card = await self._board_call("get", target)
            if card is None or str(card.get("queue_state") or "") == "queued":
                # Queued (a full project) or gone: the queue's own words, or
                # nothing to say.
                return
            await self._board_call("update", target,
                                   {"dispatch_error": detail}, bump=False)
            self._log_card_event(card, "card_dispatch_failed", error=detail)
        except Exception:
            logger.warning("preparing the worktree for card %s failed",
                           cid[:8], exc_info=True)
            try:
                await self._fail_prepare(cid, worktrees.ADD_FAILED_REFUSAL,
                                         replay)
            except Exception:
                logger.debug("could not note the failure on card %s",
                             cid[:8], exc_info=True)
        finally:
            self._pop_preparing(cid, token)
            try:
                await self._publish_board()
            except Exception:
                logger.debug("board publish after preparing failed",
                             exc_info=True)

    async def _worktree_base(self, root: str) -> str:
        """Where a new card branch starts. `origin/HEAD` after the fetch —
        unless local `main` already contains it (`merge-base
        --is-ancestor`), in which case `main`, so the person's unpushed
        work is on the base. No remote: `main`, else `HEAD`."""
        origin = ""
        got, out, _w = await self._run_git(worktrees.argv_base_ref(root), root)
        if got:
            origin = worktrees.base_from(out.decode("utf-8", "replace"))
        has_main, _o, _w = await self._run_git(
            worktrees.argv_branch_exists(root, "main"), root)
        if origin and has_main:
            behind, _o, _w = await self._run_git(
                worktrees.argv_is_ancestor(root, origin, "main"), root)
            return "main" if behind else origin
        if origin:
            return origin
        return "main" if has_main else "HEAD"

    async def _setup_script_refusal(self, root: str, tool: str, *,
                                    rel: str = worktrees.SETUP_SCRIPT) -> str:
        """The gate on the person's setup script, checked **before** any
        folder is made. `""` where there is no script or it may run; the
        words otherwise — never a silent skip.

        It runs only when the project already carries the person's trust
        decision for the assistant being started (`trust_marks.root_trusted`),
        and only when it is a regular file, untracked by git (a tracked
        script is whatever the last pull made it), owned by the person Dark
        Army runs as and writable by nobody else. The file rules are
        `_setup_script_file_check`, git's are `_setup_script_git_refusal`;
        both are taken again at the moment the script runs.
        """
        loop = asyncio.get_running_loop()
        verdict = await loop.run_in_executor(
            None, self._setup_script_file_check, root, rel)
        if verdict is None:
            return ""
        if verdict:
            return verdict
        trusted = await loop.run_in_executor(
            None, trust_marks.root_trusted, root, tool)
        if not trusted:
            return worktrees.SETUP_UNTRUSTED_REFUSAL
        return await loop.run_in_executor(
            None, self._setup_script_git_refusal, root, rel)

    def _setup_script_git_refusal(self, root: str,
                                  rel: str = worktrees.SETUP_SCRIPT) -> str:
        """The git half of the setup script's gate, **executor only**, taken
        at the Start and again immediately before the script runs (a pull in
        the gap is judged afresh). `""` when git says the script is nobody's
        but the person's; the refusal words otherwise. Fails closed: any git
        call that errs or times out refuses.

        Two looks, because spelling cannot be trusted on this disk:

        - `git status` must say **positively** that the script is untracked
          or ignored (`worktrees.setup_status_allows`); a tracked script, an
          ASCII case variant under `core.ignorecase`, or a submodule prints
          nothing, and nothing is refused.
        - Then the index is compared with the disk **by inode**: every index
          entry that could be the script under another spelling
          (`worktrees.index_script_candidates` — a first component that
          NFKC-casefolds to `.dark-army`, e.g. `.darK-army` with U+212A
          KELVIN SIGN, which git's ASCII-only `core.ignorecase` does not
          fold, or any spelling with `core.ignorecase` off) is `lstat`'d, and
          one that is the script's own file (`st_dev`, `st_ino`) is tracked
          whatever git's status said (`worktrees.tracked_as_script`).
          That listing has its own cap, `worktrees.MAX_INDEX_DOTFILES_BYTES`
          (a Yarn cache alone passes the shared one), and over it the words
          say so (`worktrees.SETUP_TOO_MANY_HIDDEN_REFUSAL`).
        """
        script = worktrees.setup_script_path(root, rel)
        refusal = worktrees.SETUP_UNSAFE_REFUSAL.format(script)
        ok, out, _w = self._git_blocking(
            worktrees.argv_setup_status(root, rel), root)
        if not ok or not worktrees.setup_status_allows(out, rel):
            return refusal
        ok, out, why = self._git_blocking(
            worktrees.argv_index_dotfiles(root), root,
            limit=worktrees.MAX_INDEX_DOTFILES_BYTES)
        if not ok:
            if why == work_record.TOO_MUCH_REASON:
                return worktrees.SETUP_TOO_MANY_HIDDEN_REFUSAL.format(script)
            return refusal
        try:
            mine = os.lstat(script)
        except OSError:
            return refusal
        ids = []
        for entry in worktrees.index_script_candidates(out, rel):
            try:
                info = os.lstat(os.path.join(root, entry))
            except OSError:
                continue
            ids.append((info.st_dev, info.st_ino))
        if worktrees.tracked_as_script((mine.st_dev, mine.st_ino), ids):
            return refusal
        return ""

    @staticmethod
    def _setup_script_file_check(root: str, rel: str = worktrees.SETUP_SCRIPT):
        """The file half of the setup script's gate, **executor only**, and
        re-made at the moment it runs. `None`: no script. `""`: it may run.
        The refusal words otherwise.

        The `.dark-army` folder must be a real directory (never a link — a
        repository can commit `.dark-army` as a link to a tracked folder, and
        `lstat` of the script alone sees only its last part), owned by the
        person, writable by nobody else and not itself a repository (no
        `.git` entry of any kind inside it); the script a regular file with
        the same ownership rule; and its realpath exactly the realpath'd
        root's own `.dark-army/worktree-setup.sh`. Before all of those, the
        project and the folder must be on an APFS disk
        (`setup_volume_type`, `worktrees.SETUP_VOLUME_REFUSAL`).
        """
        script = worktrees.setup_script_path(root, rel)
        try:
            info = os.lstat(script)
        except OSError:
            return None
        # APFS alone, for both the project and the script's folder, and a
        # disk whose kind cannot be read is refused: on Mac OS Extended a
        # tracked `.dar\u200ck-army/…` is stored as `.dark-army/…`, so the
        # index checks below could not see it (`docs/card-worktrees.md`).
        for where in (root, os.path.dirname(script)):
            if not worktrees.setup_volume_allows(setup_volume_type(where)):
                return worktrees.SETUP_VOLUME_REFUSAL.format(script)
        refusal = worktrees.SETUP_UNSAFE_REFUSAL.format(script)
        uid = os.getuid()
        try:
            folder = os.lstat(os.path.dirname(script))
        except OSError:
            return refusal
        if not worktrees.setup_folder_safe(folder.st_mode, folder.st_uid, uid):
            return refusal
        # `.dark-army` must not itself be a repository — a `.git` file (a
        # submodule's gitlink, left behind when the upstream dropped the
        # submodule and ignored the folder) or a `.git` folder (a nested
        # clone). Git never looks inside a nested repository, so its
        # `!! .dark-army/` would vouch for a script somebody else wrote.
        if os.path.lexists(os.path.join(os.path.dirname(script), ".git")):
            return refusal
        if not stat_mod.S_ISREG(info.st_mode) \
                or not worktrees.setup_script_safe(info.st_uid, info.st_mode,
                                                   uid):
            return refusal
        if os.path.realpath(script) != worktrees.setup_script_where(
                os.path.realpath(root), rel):
            return refusal
        return ""

    async def _prepare_worktree(self, head: dict, root: str) -> tuple:
        """`(path, branch, error)` — the head card's worktree made ready, or
        the words saying why not. Loop; the git calls hop to the executor
        through `_run_git`, the file work through `_finish_worktree`."""
        cid = str(head.get("id") or "")
        root = dispatch.normalise_root(root)
        path = worktrees.worktree_dir(root, cid)
        branch = str(head.get("worktree_branch") or "") \
            or worktrees.branch_name(head)
        loop = asyncio.get_running_loop()
        folder = os.path.join(root, worktrees.WORKTREES_DIR)
        if await loop.run_in_executor(None, os.path.islink, folder):
            return path, branch, worktrees.WORKTREES_SYMLINK_REFUSAL.format(
                folder)
        refusal = await self._setup_script_refusal(
            root, str(head.get("tool") or ""))
        if refusal:
            return path, branch, refusal
        ok, out, _why = await self._run_git(
            worktrees.argv_worktree_list(root), root)
        listed = worktrees.parse_worktree_list(
            out.decode("utf-8", "replace")) if ok else set()
        registered = os.path.realpath(path) in listed
        present = registered and await loop.run_in_executor(
            None, os.path.isdir, path)
        if not present:
            if registered:
                # Registered but gone from disk (removed by hand): git would
                # refuse the add, and a recorded path that is not a folder is
                # never reused — prepare-record-refuse would loop for ever.
                await self._run_git(worktrees.argv_worktree_prune(root), root)
            # A folder made new has carried nothing: a manifest left by a
            # folder that vanished without a release would read the fresh
            # HEAD bytes as edits and keep the old pack.
            try:
                os.unlink(worktrees.pack_manifest_path(
                    root, self._pack_card_id(path)))
            except OSError:
                pass  # no stale manifest
            # A failed fetch is one log line: the base is then what is local.
            has_remote, _o, _w = await self._run_git(
                worktrees.argv_remote_url(root), root)
            if has_remote:
                fetched, _o, why = await self._run_git(
                    worktrees.argv_fetch(root), root,
                    timeout=worktrees.FETCH_TIMEOUT_SECONDS)
                if not fetched:
                    logger.info("card %s: fetch failed in %s (%s); starting "
                                "from what is local", cid[:8], root, why)
            base = await self._worktree_base(root)
            existing, _o, _w = await self._run_git(
                worktrees.argv_branch_exists(root, branch), root)
            added, _o, why = await self._run_git(
                worktrees.argv_worktree_add(root, path, branch, base,
                                            existing=existing),
                root, timeout=worktrees.WORKTREE_ADD_TIMEOUT_SECONDS)
            if not added:
                logger.info("card %s: git worktree add refused in %s (%s)",
                            cid[:8], root, why)
                return path, branch, worktrees.ADD_FAILED_REFUSAL
        got, out, _w = await self._run_git(
            worktrees.argv_exclude_path(root), root)
        exclude = out.decode("utf-8", "replace").strip() if got else ""
        if exclude and not os.path.isabs(exclude):
            exclude = os.path.join(root, exclude)
        error = await loop.run_in_executor(
            None, functools.partial(self._finish_worktree, root, path, cid,
                                    branch, exclude))
        return path, branch, error

    def _finish_worktree(self, root: str, path: str, card_id: str,
                         branch: str, exclude: str) -> str:
        """The worktree's file work, **executor only**: the exclude line,
        the trust copies, the person's setup script (already judged by
        `_setup_script_refusal`). `""` when ready, else the words."""
        if exclude:
            try:
                existing = ""
                if os.path.exists(exclude):
                    with open(exclude, encoding="utf-8") as handle:
                        existing = handle.read()
                text = worktrees.exclude_text(existing)
                if text is not None:
                    os.makedirs(os.path.dirname(exclude), exist_ok=True)
                    with open(exclude, "w", encoding="utf-8") as handle:
                        handle.write(text)
            except (OSError, UnicodeError):
                logger.info("could not add %s to %s", worktrees.EXCLUDE_LINE,
                            exclude, exc_info=True)
        # An uncommitted pack update on the main checkout rides into the new
        # folder, kept off the card's branch (`docs/card-worktrees.md`, *The
        # pack copies*). A failure is one log line and never a refusal.
        note = self._sync_pack_copies(root, path, self._pack_card_id(path))
        if note:
            logger.info("card %s: %s", str(card_id)[:8], note)
        trust_marks.mark(root, path)
        return self._run_worktree_setup(root, path, card_id, branch)

    #: How the manifest is opened: never through a symbolic link planted at
    #: its name. A carried file is written to a fresh temp name instead
    #: (`_write_pack_file`), so a hard link planted at its name is never
    #: written through.
    _PACK_COPY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    _PACK_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

    @staticmethod
    def _pack_card_id(path: str) -> str:
        """The id part of a worktree folder's name (`card-<id8>`), so a
        batch's shared folder always finds its head's manifest."""
        name = os.path.basename(str(path).rstrip(os.sep))
        return name[len(worktrees.FOLDER_PREFIX):] \
            if name.startswith(worktrees.FOLDER_PREFIX) else name

    @staticmethod
    def _open_regular(full: str):
        """A binary read handle on a regular file: opened `O_NOFOLLOW |
        O_NONBLOCK` (a FIFO never blocks the open) and `fstat`ed on the
        handle, so what is read is what was checked. `OSError` otherwise."""
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{full} is not a regular file")
        except OSError:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb")

    @classmethod
    def _sweep_pack_temps(cls, folder: str) -> None:
        """Remove temp files a killed write left in `folder` (regular files
        whose name begins with the exact temp prefix). Executor."""
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            if not (name.startswith(".dark-army-pack-")
                    and name.endswith(".tmp")):
                continue
            full = os.path.join(folder, name)
            try:
                if stat_mod.S_ISREG(os.lstat(full).st_mode):
                    os.unlink(full)
            except OSError:
                pass  # already gone, or not ours to remove

    @staticmethod
    def _pack_file_digest(full: str):
        """sha256 of a regular, non-link file read in chunks; `""` when
        absent; `None` for anything else (a link, a folder), an unreadable
        file or one over `MAX_PACK_COPY_BYTES` (never read whole).
        Executor."""
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            return ""
        except OSError:
            return None
        if not stat_mod.S_ISREG(st.st_mode) \
                or st.st_size > worktrees.MAX_PACK_COPY_BYTES:
            return None
        try:
            digest = hashlib.sha256()
            with BoardVerbsMixin._open_regular(full) as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _read_pack_manifest(root: str, card_id: str) -> list:
        """The manifest's rows, `[]` when missing or malformed. Executor."""
        from dark_army_menubar import pack_install
        manifest = worktrees.pack_manifest_path(root, card_id)
        try:
            with BoardVerbsMixin._open_regular(manifest) as handle:
                data = handle.read(worktrees.MAX_PACK_COPY_BYTES)
        except OSError:
            return []
        return worktrees.parse_manifest(data.decode("utf-8", "replace"),
                                        pack_install.is_pack_path)

    @classmethod
    def _write_pack_file(cls, dest: str, data: bytes, mode: int) -> None:
        """Write `data` at `dest` through a new temp file in its folder
        (`O_EXCL | O_NOFOLLOW`) and `os.replace` it over the name: a link
        (symbolic or hard) at `dest` is replaced, never written through.
        Raises `OSError`."""
        tmp = os.path.join(os.path.dirname(dest),
                           f".dark-army-pack-{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, cls._PACK_TEMP_FLAGS, mode & 0o777)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), mode & 0o777)
                handle.write(data)
            os.replace(tmp, dest)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # the temp name is already gone
            raise

    def _sync_pack_copies(self, root: str, path: str, card_id: str) -> str:
        """Carry the main checkout's **uncommitted pack update** into a card's
        worktree and keep it off the card's branch. **Executor only**; `""`
        or one sentence for the log, and it never raises past this boundary.

        Gated on the project's pack-ledger row (Dark Army's own checkout
        and a project that never took the pack carry nothing). Every git
        call in the main checkout is a read (`status`); every write runs in
        the worktree on its own index. Changed files are copied, new ones
        are marked intent-to-add first, removed ones unlinked, then all are
        marked skip-worktree; the manifest records what was written. A
        file whose bytes no longer match the manifest, or that differs from
        HEAD in the folder with no manifest row, was edited by the previous
        run and is left alone."""
        try:
            return self._carry_pack_copies(root, path, card_id)
        except Exception:  # noqa: BLE001 - the documented boundary: a failed carry never refuses a Start
            logger.info("card %s: carrying the pack update failed",
                        str(card_id)[:8], exc_info=True)
            return "could not carry the uncommitted pack update into its folder"

    def _carry_pack_copies(self, root: str, path: str,
                           card_id: str) -> str:
        from dark_army_menubar import pack_install, pack_ledger
        if pack_ledger.entry(root) is None:
            return ""
        short = str(card_id)[:8]
        failed = "could not read the uncommitted pack update in the main checkout"
        ok, out, _why = self._git_blocking(
            worktrees.argv_pack_status(root, pack_install.pack_pathspecs()),
            root)
        if not ok:
            return failed

        def exists(rel: str):
            try:
                st = os.lstat(os.path.join(root, rel))
            except FileNotFoundError:
                return False
            except OSError:
                return None
            return True if stat_mod.S_ISREG(st.st_mode) else None

        plan = worktrees.pack_copy_plan(
            worktrees.parse_status_z(out), pack_install.is_pack_path, exists)
        if not plan:
            return ""
        previous = {row["path"]: row for row in
                    self._read_pack_manifest(root, card_id)}
        wt_real = os.path.realpath(path)
        # A path the manifest does not know may carry an uncommitted edit the
        # previous run left in this folder: ask the worktree what differs
        # from its HEAD and leave those alone.
        fresh_paths = [i["path"] for i in plan if i["path"] not in previous]
        edited_unknown: set = set()
        if fresh_paths:
            ok, dirty, _why = self._git_blocking(
                worktrees.argv_pack_status(path, fresh_paths), path)
            if not ok:
                logger.info("card %s: could not tell which pack files the "
                            "folder already changed; carrying only the "
                            "files a previous carry recorded", short)
                edited_unknown = set(fresh_paths)
            else:
                edited_unknown = {p for _c, p in
                                  worktrees.parse_status_z(dirty)}
        # `git status` never lists a skip-worktree file: a marked path the
        # manifest does not name (a lost manifest) is unknown too.
        ok, listing_marked, _why = self._git_blocking(
            worktrees.argv_ls_files_marked(
                path, pack_install.pack_pathspecs()), path)
        if not ok:
            edited_unknown |= {i["path"] for i in plan}
        else:
            edited_unknown |= {p for p in
                               worktrees.parse_skipped(listing_marked)
                               if p not in previous}
        keep = []
        for item in plan:
            rel = item["path"]
            row = previous.get(rel)
            if row is None:
                if rel in edited_unknown:
                    logger.info("card %s: %s has changes in its folder; not "
                                "carried", short, rel)
                    continue
            else:
                now = self._pack_file_digest(os.path.join(path, rel))
                if now != row["sha256"]:
                    logger.info("card %s: %s was edited in its folder; not "
                                "refreshed", short, rel)
                    continue
            keep.append(item)
        plan = keep
        if not plan:
            return ""
        ok, listing, _why = self._git_blocking(
            worktrees.argv_ls_files(path, [i["path"] for i in plan]), path)
        if not ok:
            return "could not list the card folder's tracked files"
        tracked = {e for e in listing.decode("utf-8", "surrogateescape")
                   .split("\0") if e}

        def contained(dest: str) -> bool:
            parent = os.path.realpath(os.path.dirname(dest))
            return parent == wt_real or parent.startswith(wt_real + os.sep)

        rows: dict = {}
        problem = ""
        try:
            for item in plan:
                rel, dest = item["path"], os.path.join(path, item["path"])
                if not contained(dest):
                    logger.info("card %s: %s would land outside its folder; "
                                "skipped", short, rel)
                    continue
                if item["action"] == "delete":
                    if rel not in tracked:
                        continue
                    try:
                        if stat_mod.S_ISREG(os.lstat(dest).st_mode):
                            os.unlink(dest)
                    except FileNotFoundError:
                        pass
                    rows[rel] = {"path": rel, "kind": "deleted", "sha256": ""}
                    continue
                src = os.path.join(root, rel)
                st = os.lstat(src)
                if not stat_mod.S_ISREG(st.st_mode) \
                        or st.st_size > worktrees.MAX_PACK_COPY_BYTES:
                    logger.info("card %s: %s is not carried (not a plain file "
                                "or too large)", short, rel)
                    continue
                with self._open_regular(src) as handle:
                    data = handle.read(worktrees.MAX_PACK_COPY_BYTES + 1)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if not contained(dest):
                    continue
                self._sweep_pack_temps(os.path.dirname(dest))
                self._write_pack_file(dest, data, st.st_mode)
                prior = previous.get(rel)
                is_new = rel not in tracked or (
                    prior is not None and prior["kind"] == "new")
                rows[rel] = {"path": rel, "kind": "new" if is_new else "tracked",
                             "sha256": hashlib.sha256(data).hexdigest()}
        except OSError:
            logger.info("card %s: a pack file could not be carried", short,
                        exc_info=True)
            problem = "could not carry every uncommitted pack file"
        # Whatever was written is marked, even after a failure, so nothing
        # written is left showing as the agent's own change. An intent-to-add
        # that fails must not take the other marks with it (git aborts the
        # whole `update-index` over one path it cannot mark).
        new_paths = [r for r, row in rows.items()
                     if row["kind"] == "new" and r not in tracked]
        if new_paths:
            ok, _o, _why = self._git_blocking(
                worktrees.argv_intent_to_add(path, new_paths), path)
            if not ok:
                problem = problem or "could not mark the new pack files"
                self._undo_pack_copies(path, new_paths, rows, set(), short)
        marked = list(rows)
        if marked:
            ok, _o, _why = self._git_blocking(
                worktrees.argv_skip_worktree(path, marked, True), path)
            if not ok:
                problem = problem or "could not mark the carried pack files"
                self._undo_pack_copies(path, marked, rows, tracked, short)
        merged = dict(previous)
        merged.update(rows)
        if merged:
            try:
                self._write_pack_file(
                    worktrees.pack_manifest_path(root, card_id),
                    worktrees.manifest_text(list(merged.values())).encode(
                        "utf-8"), 0o600)
            except OSError:
                logger.info("could not write the pack manifest for card %s",
                            short, exc_info=True)
                problem = problem or "could not record the carried pack files"
        logger.info("card %s: carried %d uncommitted pack file(s) into %s",
                    short, len(rows), path)
        return problem

    def _undo_pack_copies(self, path: str, rels: list, rows: dict,
                          tracked: set, short: str) -> None:
        """A carried file that could not be marked is never left committable:
        a tracked or deleted one goes back to its HEAD bytes, a new one is
        removed (and its intent-to-add entry dropped). `rows` loses them.
        Executor."""
        wt_real = os.path.realpath(path)
        gone = []
        for rel in list(rels):
            dest = os.path.join(path, rel)
            try:
                parent = os.path.realpath(os.path.dirname(dest))
                if not (parent == wt_real
                        or parent.startswith(wt_real + os.sep)):
                    continue
                row = rows.get(rel)
                if row is None:
                    continue
                if row["kind"] == "new" and rel not in tracked:
                    try:
                        if stat_mod.S_ISREG(os.lstat(dest).st_mode):
                            os.unlink(dest)
                    except FileNotFoundError:
                        pass
                    gone.append(rel)
                else:
                    ok, tree, _why = self._git_blocking(
                        worktrees.argv_ls_tree(path, rel), path)
                    head_mode = worktrees.parse_tree_mode(tree) if ok else ""
                    if head_mode == "120000":
                        logger.info("card %s: %s is a link at HEAD; left as "
                                    "it is", short, rel)
                    elif not head_mode:
                        logger.info("card %s: could not put %s back", short,
                                    rel)
                    else:
                        ok, blob, _why = self._git_blocking(
                            worktrees.argv_cat_blob(path, rel), path,
                            limit=worktrees.MAX_PACK_COPY_BYTES + 1)
                        if ok:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            self._write_pack_file(
                                dest, blob,
                                0o755 if head_mode == "100755" else 0o644)
                        else:
                            logger.info("card %s: could not put %s back",
                                        short, rel)
                rows.pop(rel, None)
            except OSError:
                logger.info("card %s: could not undo the copy of %s", short,
                            rel, exc_info=True)
        if gone:
            self._git_blocking(worktrees.argv_rm_cached(path, gone), path)

    def _unskip_edited_pack_copies(self, root: str, path: str, card_id: str):
        """Executor only. Un-mark every carried file whose bytes now differ
        from the manifest (an edited copy, a removed file that exists again)
        so `git worktree remove` refuses and keeps the folder — nothing an
        agent wrote is thrown away. An untouched copy stays marked: an
        un-marked intent-to-add entry would show as added and block the
        remove. Returns the paths un-marked, or **None when it cannot tell**
        (the folder holds skip-worktree entries but the manifest is missing
        or malformed, git failed, or the un-mark failed): the release keeps
        the folder then. A folder with no marked entries gives `[]`."""
        try:
            from dark_army_menubar import pack_install
            short = str(card_id)[:8]
            ok, listing, _why = self._git_blocking(
                worktrees.argv_ls_files_marked(
                    path, pack_install.pack_pathspecs()), path)
            if not ok:
                return None
            marked = worktrees.parse_skipped(listing)
            if not marked:
                return []
            rows = self._read_pack_manifest(root, card_id)
            if not rows:
                return None
            mismatched = []
            known = set()
            for row in rows:
                known.add(row["path"])
                now = self._pack_file_digest(os.path.join(path, row["path"]))
                if now is None or now != row["sha256"]:
                    mismatched.append(row["path"])
            # A marked entry the manifest does not name is nobody's record:
            # make it visible too, so git decides.
            mismatched += [p for p in marked if p not in known]
            if not mismatched:
                return []
            ok, _o, _why = self._git_blocking(
                worktrees.argv_skip_worktree(path, mismatched, False), path)
            if not ok:
                logger.info("card %s: could not un-mark edited pack copies",
                            short)
                return None
            logger.info("card %s: edited pack copies left visible to git: %s",
                        short, ", ".join(mismatched))
            return mismatched
        except Exception:  # noqa: BLE001 - the documented boundary: "cannot tell" keeps the folder
            logger.info("card %s: could not check the carried pack copies",
                        str(card_id)[:8], exc_info=True)
            return None

    #: How the setup log is opened at both write sites: never through a
    #: symbolic link planted at its name, private to the person.
    _SETUP_LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW

    def _run_worktree_setup(self, root: str, path: str, card_id: str,
                            branch: str, *, rel: str = worktrees.SETUP_SCRIPT,
                            log_path: Optional[str] = None,
                            timeout: Optional[float] = None) -> str:
        """Run `<root>/.dark-army/worktree-setup.sh` in the new worktree, if
        the person wrote one. `""` on success or when there is none; the
        refusal words otherwise. Executor only.

        Re-checked as a regular file (a symlink is refused, never followed),
        run as `/bin/bash <script>` with the argv alone. Its own session, so
        a timeout kills the whole group (`npm install` included). Output
        goes to `worktrees.setup_log_path`, opened `O_NOFOLLOW` at 0600 and
        cut to its last `MAX_SETUP_LOG_BYTES`.

        `rel`, `log_path` and `timeout` let the merge's check script
        (`merges.MERGE_CHECK_SCRIPT`) run under exactly the same rules
        (`_exec_setup_script`); the defaults are the setup script's own.
        """
        refusal, failure, log = self._exec_setup_script(
            root, path, card_id, branch, rel=rel, log_path=log_path,
            timeout=timeout)
        if refusal:
            return refusal
        if failure:
            return worktrees.SETUP_FAILED_REFUSAL.format(failure, log)
        return ""

    def _exec_setup_script(self, root: str, path: str, card_id: str,
                           branch: str, *, rel: str = worktrees.SETUP_SCRIPT,
                           log_path: Optional[str] = None,
                           timeout: Optional[float] = None) -> tuple:
        """`(refusal, failure, log_path)` for one run of a project script.
        Executor only. `refusal` is the gate's words (no run happened);
        `failure` is `exit N`, `timed out` or `could not run` (it ran or
        tried, and the log is at `log_path`); both empty is success, or no
        script at all."""
        script = worktrees.setup_script_path(root, rel)
        log_path = log_path or worktrees.setup_log_path(root, card_id)
        bound = float(timeout or worktrees.SETUP_TIMEOUT_SECONDS)
        verdict = self._setup_script_file_check(root, rel)
        if verdict is None:
            return "", "", log_path
        if verdict:
            return verdict, "", log_path
        # And git's half again, immediately before it runs: a pull between
        # the Start and now is judged afresh.
        verdict = self._setup_script_git_refusal(root, rel)
        if verdict:
            return verdict, "", log_path
        try:
            fd = os.open(log_path, self._SETUP_LOG_FLAGS, 0o600)
        except OSError:
            logger.info("could not open the setup log %s", log_path,
                        exc_info=True)
            return "", "could not write its log", log_path
        code = "timed out"
        returncode = None
        try:
            with os.fdopen(fd, "wb") as log:
                proc = subprocess.Popen(
                    ["/bin/bash", script], cwd=path,
                    env=worktrees.setup_env(None, root, path, card_id, branch),
                    stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    returncode = proc.wait(timeout=bound)
                    code = f"exit {returncode}"
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, 9)
                    except OSError:
                        proc.kill()
                    proc.wait()
        except OSError:
            logger.info("could not run the worktree setup script in %s", path,
                        exc_info=True)
            return "", "could not run", log_path
        try:
            size = os.path.getsize(log_path)
            if size > worktrees.MAX_SETUP_LOG_BYTES:
                rfd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(rfd, "rb") as handle:
                    handle.seek(size - worktrees.MAX_SETUP_LOG_BYTES)
                    tail = handle.read()
                wfd = os.open(log_path, self._SETUP_LOG_FLAGS, 0o600)
                with os.fdopen(wfd, "wb") as handle:
                    handle.write(tail)
        except OSError:
            logger.debug("could not trim %s", log_path, exc_info=True)
        if returncode == 0:
            logger.info("card %s: worktree script %s ran in %s",
                        card_id[:8], rel, path)
            return "", "", log_path
        logger.info("card %s: worktree script %s failed (%s)",
                    card_id[:8], rel, code)
        return "", code, log_path

    def _session_inside(self, path: str) -> bool:
        """Whether a live session in the cached agents snapshot works in
        `path` or below it. Blocking (realpaths); executor. The release's
        own reading of "nothing runs here" — `link_state` alone misses a
        card whose bind window ran out or that was dragged out of In
        progress with its terminal still open in the folder."""
        snapshot = getattr(self, "_agents_snapshot_cache", None) or {}
        if not path or not isinstance(snapshot, dict):
            return False
        live = self._board_live_ids(snapshot)
        base = os.path.realpath(str(path)).rstrip(os.sep)
        for group in snapshot.values():
            if not isinstance(group, list):
                continue
            for row in group:
                if not isinstance(row, dict) or row.get("session_id") not in live:
                    continue
                cwd = str(row.get("cwd") or "")
                if not cwd:
                    continue
                where = os.path.realpath(cwd)
                if where == base or where.startswith(base + os.sep):
                    return True
        return False

    @staticmethod
    def _worktree_shared(card: dict, cards) -> tuple:
        """`(still_open, finished)` over the other cards sharing `card`'s
        folder — the same `worktree_path`, or the same `batch_id`. Pure.
        `still_open` when any of them is not in Done or has a live or
        dispatching link: the folder is theirs too and must stay.
        `finished` is the rest, cleared with the card on a release."""
        card = card or {}
        cid = str(card.get("id") or "")
        path = str(card.get("worktree_path") or "")
        bid = str(card.get("batch_id") or "")
        finished = []
        for other in cards or ():
            if str(other.get("id") or "") == cid:
                continue
            same = (path and str(other.get("worktree_path") or "") == path) \
                or (bid and str(other.get("batch_id") or "") == bid)
            if not same:
                continue
            if str(other.get("column_name") or "") != "done" \
                    or str(other.get("link_state") or "") in (
                        "live", "dispatching"):
                return True, []
            finished.append(other)
        return False, finished

    def _worktree_note(self, card: dict, by_id: Optional[dict] = None, *,
                       cards=None) -> str:
        """The line a card draws about its folder, or `""`. Executor.

        Derived, never remembered, so it survives a restart and goes the
        moment its reason does: preparing while the task runs; **kept** only
        on a Done card whose link is not live, whose folder is still there,
        with no live session inside it and no release already on its way —
        the state a refused `git worktree remove` leaves. A reset, a new
        run, or a folder removed by hand all make it false by themselves.
        """
        cid = str((card or {}).get("id") or "")
        if not cid:
            return ""
        if cid in (getattr(self, "_worktree_preparing", None) or {}):
            return worktrees.PREPARING_NOTE
        path = str(card.get("worktree_path") or "")
        if not path or str(card.get("column_name") or "") != "done":
            return ""
        if str(card.get("merge_state") or "") in merges.FIX_STATES:
            # The card's own line says what to do (`merge_line`).
            return ""
        if str(card.get("link_state") or "") in ("live", "dispatching"):
            return ""
        if cid in (getattr(self, "_worktree_release_queue", None) or []) \
                or cid in (getattr(self, "_worktree_releasing", None) or set()):
            return ""
        # Kept because a card sharing the folder is still at work is not
        # "unsaved changes": no note.
        others = cards if cards is not None else (by_id or {}).values()
        if self._worktree_shared(card, others)[0]:
            return ""
        if not os.path.isdir(path) or self._session_inside(path):
            return ""
        return worktrees.KEPT_NOTE.format(path)

    def _consider_worktree_release(self, card: dict) -> None:
        """Queue a Done card for its folder's release. **Executor, no
        I/O** — `_consider_work_record`'s seam; the decision is re-made on
        the loop against the card as it is then."""
        card = card if isinstance(card, dict) else {}
        cid = str(card.get("id") or "")
        if not cid or not str(card.get("worktree_path") or ""):
            return
        if str(card.get("column_name") or "") != "done":
            return
        queue = getattr(self, "_worktree_release_queue", None)
        if queue is None:
            queue = []
            self._worktree_release_queue = queue
        if cid not in queue:
            queue.append(cid)

    def _consider_standing_worktree(self, card: dict) -> None:
        """Once per process per (card, folder): a Done card that still names
        a folder and whose link is not live is looked at again — the release
        after a restart, the clear of a folder removed by hand. Executor, no
        I/O; the set is this process's memory only."""
        card = card if isinstance(card, dict) else {}
        cid = str(card.get("id") or "")
        path = str(card.get("worktree_path") or "")
        if not cid or not path or str(card.get("column_name") or "") != "done":
            return
        if str(card.get("link_state") or "") in ("live", "dispatching"):
            return
        seen = getattr(self, "_worktree_considered", None)
        if seen is None:
            seen = set()
            self._worktree_considered = seen
        if (cid, path) in seen:
            return
        seen.add((cid, path))
        self._consider_worktree_release(card)

    def _consider_worktree_prunes(self) -> None:
        """Once per process per enrolled root: queue the root for the
        stale-registration sweep (`_prune_stale_worktrees`) when it is a git
        project (`<root>/.git`, a folder or a linked worktree's file).
        Executor, one `os.path.exists` per new root, no git call; both
        attributes are replaced, never mutated."""
        seen = set(getattr(self, "_worktree_prune_seen", None) or ())
        fresh = [r for r in enrollment.enrolled_roots() if r not in seen]
        if not fresh:
            return
        queue = list(getattr(self, "_worktree_prune_queue", None) or [])
        for root in fresh:
            seen.add(root)
            if os.path.exists(os.path.join(root, ".git")) \
                    and root not in queue:
                queue.append(root)
        self._worktree_prune_seen = seen
        self._worktree_prune_queue = queue

    def _prune_root_busy(self, root: str) -> bool:
        """Whether a card's folder is being prepared in `root`: its `git
        worktree add` may be mid-flight, so the sweep waits for a later
        round rather than pruning beside it."""
        for entry in (getattr(self, "_worktree_preparing", None)
                      or {}).values():
            if dispatch.normalise_root(
                    str((entry or {}).get("root") or "")) == root:
                return True
        return False

    async def _prune_stale_worktrees(self, root: str) -> None:
        """Forget git's registrations whose folders are gone in one project.
        Reads git's own list first and refuses the whole prune when git
        would forget an entry whose folder is still a directory (its `.git`
        marker file was deleted): one log line, nothing done. Never removes
        a folder, never names a path to git, never raises."""
        try:
            ok, out, reason = await self._run_git(
                worktrees.argv_worktree_list(root), root)
            if not ok:
                logger.info("worktrees: could not list %s: %s", root, reason)
                return
            entries = worktrees.parse_worktree_entries(
                out.decode("utf-8", "replace"))
            loop = asyncio.get_running_loop()

            def decide():
                return worktrees.prune_decision(entries, os.path.isdir)

            missing, blocked = await loop.run_in_executor(None, decide)
            if blocked:
                logger.info(worktrees.PRUNE_BLOCKED_LINE.format(
                    root, blocked[0]))
                return
            if not missing:
                logger.debug("worktrees: nothing to prune in %s", root)
                return
            ok, _out, reason = await self._run_git(
                worktrees.argv_worktree_prune(root), root)
            if not ok:
                logger.info("worktrees: pruning %s failed: %s", root, reason)
                return
            logger.debug("worktrees: forgot %d dead registration(s) in %s",
                         len(missing), root)
        except Exception:
            logger.warning("pruning the worktrees of %s failed", root,
                           exc_info=True)

    def _forget_worktree_state(self, card_id: str) -> None:
        """A new run of this card: its old consideration is stale. Replaced,
        never mutated."""
        seen = getattr(self, "_worktree_considered", None) or set()
        self._worktree_considered = {k for k in seen if k[0] != card_id}

    def _defer_worktree_release(self, card: dict, deleting: bool) -> None:
        """Try again on a later pass. A deleted card is remembered whole
        (`_worktree_orphans`) — there is no row to re-read."""
        cid = str(card.get("id") or "")
        if deleting:
            orphans = dict(getattr(self, "_worktree_orphans", None) or {})
            orphans[cid] = dict(card, link_state="ended")
            self._worktree_orphans = orphans
            return
        queue = getattr(self, "_worktree_release_queue", None)
        if queue is None:
            queue = []
            self._worktree_release_queue = queue
        if cid not in queue:
            queue.append(cid)

    async def _kick_worktree_releases(self) -> None:
        """Start the queued releases as **one detached task**, one in flight
        — `_work_record_task`'s pattern. Nothing on the agents-push path, a
        Done write, a delete or Clear Done ever awaits a `git worktree
        remove`. Async only so it runs on the loop (`_flush_*`'s placement
        rule); it awaits nothing. A pass while one runs starts nothing (the
        queue waits for the next)."""
        if not (getattr(self, "_worktree_release_queue", None)
                or getattr(self, "_worktree_orphans", None)
                or getattr(self, "_worktree_prune_queue", None)):
            return
        task = getattr(self, "_worktree_release_task", None)
        if task is not None and not task.done():
            return
        try:
            self._worktree_release_task = asyncio.ensure_future(
                self._flush_worktree_releases())
        except RuntimeError:
            return

    async def _deleted_card_worktree(self, card: dict) -> Optional[dict]:
        """The card as its folder's release should see it, or None. Its
        recorded folder — or, where none was recorded (a setup script failed
        and the next press never came), the folder its id names under the
        root's `.worktrees/`, when that is a directory."""
        card = dict(card or {})
        cid = str(card.get("id") or "")
        # Still being prepared: `worktree add` or the setup script may be
        # running in it. The prepare task discards it itself once it finds
        # the card gone.
        if cid in (getattr(self, "_worktree_preparing", None) or {}):
            return None
        if str(card.get("worktree_path") or ""):
            return card
        root = dispatch.normalise_root(str(card.get("root") or ""))
        if not cid or not root:
            return None
        derived = worktrees.worktree_dir(root, cid)
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, os.path.isdir, derived):
            return None
        card["worktree_path"] = derived
        return card

    async def _flush_worktree_releases(self) -> None:
        """Release what the reconcile queued, and retry the deleted cards'
        folders that were still in use. Loop (as its own task); never
        raises."""
        # Re-checked before it exits, so a release queued while this one ran
        # is not left for the next push. Each round takes only what this
        # task has not handled yet: a release it deferred stays queued for
        # a later pass, so the loop is bounded by what arrives meanwhile.
        handled: set = set()
        while True:
            queue = list(getattr(self, "_worktree_release_queue", None) or [])
            orphans = dict(getattr(self, "_worktree_orphans", None) or {})
            fresh = [c for c in queue if ("card", c) not in handled]
            fresh_orphans = {k: v for k, v in orphans.items()
                             if ("orphan", k) not in handled}
            prunes = list(getattr(self, "_worktree_prune_queue", None) or [])
            fresh_prunes = [r for r in prunes
                            if ("prune", r) not in handled
                            and not self._prune_root_busy(r)]
            if not fresh and not fresh_orphans and not fresh_prunes:
                return
            self._worktree_release_queue = [c for c in queue
                                            if ("card", c) in handled]
            self._worktree_orphans = {k: v for k, v in orphans.items()
                                      if ("orphan", k) in handled}
            self._worktree_prune_queue = [r for r in prunes
                                          if r not in fresh_prunes]
            handled |= {("card", c) for c in fresh}
            handled |= {("orphan", k) for k in fresh_orphans}
            handled |= {("prune", r) for r in fresh_prunes}
            self._worktree_releasing = set(fresh)
            try:
                await self._release_batch(fresh, fresh_orphans)
            finally:
                self._worktree_releasing = set()
            for root in fresh_prunes:
                if self._prune_root_busy(root):
                    # A prepare started while the releases ran: left for
                    # the next task (still in `handled`, so this one ends).
                    self._worktree_prune_queue = list(
                        getattr(self, "_worktree_prune_queue", None)
                        or []) + [root]
                    continue
                await self._prune_stale_worktrees(root)

    async def _release_batch(self, queue: list, orphans: dict) -> None:
        for cid in queue:
            try:
                await self._maybe_release_worktree({"id": cid})
            except Exception:
                logger.warning("releasing the worktree of card %s failed",
                               cid[:8], exc_info=True)
        for cid, card in orphans.items():
            try:
                await self._maybe_release_worktree(card, deleting=True)
            except Exception:
                logger.warning("releasing the worktree of deleted card %s "
                               "failed", cid[:8], exc_info=True)

    async def _release_still_safe(self, cid: str, path: str,
                                  deleting: bool, *,
                                  merging: bool = False) -> str:
        """The release's last look, taken immediately before `git worktree
        remove`. `"ok"`; `"wait"` (a live session or a held receipt — try
        again later); or why the folder is someone's again. A deleted card
        must still be gone; a kept one must still be in Done, still name this
        folder, with its link neither live nor dispatching, and not being
        prepared."""
        card = await self._board_call("get", cid)
        if deleting:
            if card is not None:
                return "the card exists again"
        else:
            if card is None:
                return "the card is gone"
            if str(card.get("column_name") or "") != "done":
                return "the card left Done"
            if str(card.get("worktree_path") or "") != path:
                return "the card names another folder"
            if str(card.get("link_state") or "") in ("live", "dispatching"):
                return "the card is running"
        if cid in (getattr(self, "_worktree_preparing", None) or {}):
            return "the card is being prepared"
        if not merging and self._merge_hold(cid):
            return "wait"
        if cid in (getattr(self, "_spawn_shell_pids", None) or {}) \
                or cid in (getattr(self, "_spawn_pty_pids", None) or {}):
            return "wait"
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, self._session_inside, path):
            return "wait"
        return "ok"

    async def _maybe_release_worktree(self, card: dict, *,
                                      deleting: bool = False,
                                      merging: bool = False) -> bool:
        """Remove a finished card's worktree when nothing runs in it and
        nothing unsaved is in it. Loop. False when the release was deferred
        to a later pass.

        Only a card in Done (or deleted) whose link is neither `live` nor
        `dispatching`, whose folder holds **no live session's cwd** and no
        spawn receipt Dark Army still holds for it — a folder is never
        removed from under a live shell — and, for a batch, once no other
        card sharing the folder is still open or running. A deleted card
        whose link was still live is deferred and judged by the live-session
        test alone, since no row is left to watch. `git worktree remove`
        **without `--force`**: git refuses a tree with modified or untracked
        files, and that refusal keeps the folder; the card then says so
        (`_worktree_note`, derived). The branch is never deleted here — the
        card keeps the memory of it (`clear_worktree` empties the folder
        only) until a landed MERGE deletes it.

        A folder a merge or a helper holds (`_merge_hold`) is deferred, never
        removed; the merge's own cleanup passes `merging=True`.
        """
        cid = str((card or {}).get("id") or "")
        if not cid:
            return True
        current = dict(card) if deleting else await self._board_call("get", cid)
        if not current:
            return True
        path = str(current.get("worktree_path") or "")
        if not path:
            return True
        if not deleting and str(current.get("column_name") or "") != "done":
            return True
        if not merging and self._merge_hold(cid):
            self._defer_worktree_release(current, deleting)
            return False
        if str(current.get("link_state") or "") in ("live", "dispatching"):
            if deleting:
                self._defer_worktree_release(current, deleting)
                return False
            return True
        root = dispatch.normalise_root(str(current.get("root") or ""))
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, worktrees.inside, root, path):
            logger.info("card %s: %s is not under %s; not removing it",
                        cid[:8], path, root)
            return True
        held = cid in (getattr(self, "_spawn_shell_pids", None) or {}) \
            or cid in (getattr(self, "_spawn_pty_pids", None) or {})
        if held or await loop.run_in_executor(None, self._session_inside, path):
            logger.info("card %s: a session is still in %s; its release waits",
                        cid[:8], path)
            self._defer_worktree_release(current, deleting)
            return False
        cards = await self._board_call("cards") or []
        still_open, sharing = self._worktree_shared(current, cards)
        if still_open:
            return True
        if any(item[0] == cid for item in self._work_record_queue):
            await self._collect_work_record_now(cid)
        running = getattr(self, "_work_record_task", None)
        if running is not None and not running.done():
            self._defer_worktree_release(current, deleting)
            return False
        # The destructive verb re-checks at the moment it fires: every await
        # above was a gap in which the card could have been dragged out of
        # Done and started again in this very folder.
        verdict = await self._release_still_safe(cid, path, deleting,
                                                 merging=merging)
        if verdict == "wait":
            self._defer_worktree_release(current, deleting)
            return False
        if verdict != "ok":
            logger.info("card %s: %s is in use again (%s); not removing it",
                        cid[:8], path, verdict)
            return True
        if await loop.run_in_executor(None, os.path.isdir, path):
            # A scout report, a plan or a check file is git-ignored, and git
            # removes ignored files without refusing: keep the folder when
            # the crew left anything there, or when git cannot say — unless
            # every such file is already in the main checkout, byte for byte
            # (a flagged check is copied there and its original stays).
            read, listing, _why = await self._run_git(
                worktrees.argv_crew_output(path), path)
            if not read or (worktrees.holds_crew_output(listing)
                            and not await loop.run_in_executor(
                                None, _crew_output_is_copied,
                                listing, path, root)):
                logger.info("card %s: kept %s — it holds crew output git "
                            "ignores (a report, plan or check)", cid[:8], path)
                await self._publish_board()
                return True
            unmarked = await loop.run_in_executor(
                None, self._unskip_edited_pack_copies, root, path,
                self._pack_card_id(path))
            if unmarked is None:
                logger.info("card %s: kept %s — Dark Army could not tell "
                            "whether its carried pack files were edited",
                            cid[:8], path)
                await self._publish_board()
                return True
            removed, _o, why = await self._run_git(
                worktrees.argv_worktree_remove(root, path), root,
                timeout=worktrees.WORKTREE_ADD_TIMEOUT_SECONDS)
            if not removed:
                logger.info("card %s: kept %s — it holds work that was not "
                            "committed (%s)", cid[:8], path, why)
                await self._publish_board()
                return True
        for done in [current] + sharing:
            await self._board_call("clear_worktree",
                                   str(done.get("id") or ""))
        await loop.run_in_executor(None, trust_marks.unmark, path)
        try:
            os.unlink(worktrees.pack_manifest_path(
                root, self._pack_card_id(path)))
        except OSError:
            pass  # no manifest was written, or it is already gone
        logger.info("card %s: removed its worktree %s; branch %s kept on the card",
                    cid[:8], path, str(current.get("worktree_branch") or ""))
        await self._publish_board()
        return True

    # --- Review and merge (docs/card-worktrees.md, *Review and merge*) ---
    #
    # A Done card keeps the memory of its branch. Three presses act on it:
    # MERGE (armed, confirmed; the person's own verb, never an agent run),
    # Fix and Run review (one assistant each, started in the card's own
    # folder). The decisions are `merges.py`'s; every git call runs through
    # `_run_git` / `_git_blocking`; nothing here pushes.

    def _merge_hold(self, card_id: str) -> bool:
        """Whether the card's folder is spoken for by a merge or a helper
        Dark Army started in it: the release defers meanwhile. A helper's
        entry holds for the bind window, after which `_session_inside` sees
        the session itself."""
        cid = str(card_id or "")
        if cid in (getattr(self, "_merging", None) or {}):
            return True
        entry = (getattr(self, "_merge_helpers", None) or {}).get(cid)
        if entry:
            return (time.time() - float(entry.get("since") or 0.0)
                    <= dispatch.DISPATCH_BIND_WINDOW)
        return False

    def _merge_move_refusal(self, before, dest) -> str:
        """Moving a card out of Done while its merge runs would leave a
        detached folder under a card that is no longer finished: refused in
        words until the task is over."""
        before = before or {}
        cid = str(before.get("id") or "")
        if cid and cid in (getattr(self, "_merging", None) or {}) \
                and str(before.get("column_name") or "") == "done" \
                and dest and str(dest) != "done":
            return merges.MOVE_WHILE_MERGING_REFUSAL
        return ""

    def _merging_refusal(self, card_id: str, root: str) -> str:
        """`MERGE_RUNNING` / `PROJECT_MERGING`, or `""`. Pure dict lookups;
        the loop calls it again with no await before it records the merge,
        which is what makes two presses at once lose one."""
        merging = getattr(self, "_merging", None) or {}
        if str(card_id) in merging:
            return merges.MERGE_RUNNING_REFUSAL
        for other, entry in merging.items():
            if other != str(card_id) and (entry or {}).get("root") == root:
                return merges.PROJECT_MERGING_REFUSAL
        return ""

    def _manual_failed(self, path: str, *, cached: bool) -> bool:
        """Whether the check file at `path` says `Status: failed`. With
        `cached` (the snapshot path) the answer is memoised by the file's
        `(mtime, size)`, so a frame of Done cards is a stat each, not a read;
        the press always reads afresh. **Executor.**"""
        memo = getattr(self, "_manual_failed_memo", None)
        if memo is None:
            memo = self._manual_failed_memo = {}
        key = None
        if cached:
            try:
                info = os.stat(path)
                key = (info.st_mtime_ns, info.st_size)
            except OSError:
                return False
            hit = memo.get(path)
            if hit and hit[0] == key:
                return hit[1]
        resolved, refusal = self._manual_check_place(path)
        failed = bool(resolved and not refusal and manual_check.read_header(
            resolved).get("status", "") == "failed")
        if key is not None:
            if len(memo) > 2000:
                memo.clear()
            memo[path] = (key, failed)
        return failed

    def _merge_manual_refusal(self, card: dict, *, cached: bool = False) -> str:
        """The hand-check rung of the gate, read from the card and the check
        file **at the press**. **Executor.** Open steps refuse; so does a
        file whose `Status` is `failed` (recording an outcome clears the
        steps on Passed *and* Failed, so the outcome is only in the file).
        A file the place rule no longer admits counts as settled."""
        if str(card.get("manual_steps") or "").strip():
            return merges.MANUAL_OPEN_REFUSAL
        path = str(card.get("manual_check_path") or "")
        if path and self._manual_failed(path, cached=cached):
            return merges.MANUAL_FAILED_REFUSAL
        return ""

    def _merge_gate_sync(self, card, *, expected_tip: str = "",
                         busy: bool = True, cached: bool = False) -> tuple:
        """`(refusal, root, branch, path)` — whether this Done card's branch
        may be reviewed, fixed or merged right now. **Blocking; executor
        only.** The refusal is the first rung that fails, in order: board,
        card, Done, a recorded branch, not merged, the hand-check settled,
        enrolled, a git checkout, then the busy rungs (`busy=False` leaves
        those out — the Changes page asks whether the branch *could* be
        merged), then not merging, then the tip echo.

        Taken at the press and again, by the verbs that act, at the moment
        each fires (`docs/card-worktrees.md`, *Review and merge*)."""
        if self._board is None:
            return merges.BOARD_CLOSED_REFUSAL, "", "", ""
        if not isinstance(card, dict) or not card:
            return merges.NO_CARD_REFUSAL, "", "", ""
        cid = str(card.get("id") or "")
        root = dispatch.normalise_root(str(card.get("root") or ""))
        branch = str(card.get("worktree_branch") or "")
        path = str(card.get("worktree_path") or "")
        out = (root, branch, path)
        if str(card.get("column_name") or "") != "done":
            return (merges.NOT_DONE_REFUSAL,) + out
        if not branch:
            return (merges.NO_BRANCH_REFUSAL,) + out
        if str(card.get("merge_state") or "") == "merged":
            return (merges.ALREADY_MERGED_REFUSAL,) + out
        refusal = self._merge_manual_refusal(card, cached=cached)
        if refusal:
            return (refusal,) + out
        if not root or not enrollment.root_enrolled(root):
            return (merges.NOT_ENROLLED_REFUSAL,) + out
        if not self._git_checkout(root):
            return (merges.NOT_A_CHECKOUT_REFUSAL,) + out
        if busy:
            if str(card.get("link_state") or "") in ("live", "dispatching") \
                    or cid in (getattr(self, "_spawn_shell_pids", None) or {}) \
                    or cid in (getattr(self, "_spawn_pty_pids", None) or {}):
                return (merges.CARD_LIVE_REFUSAL,) + out
            if cid in (getattr(self, "_worktree_preparing", None) or {}):
                return (merges.PREPARING_REFUSAL,) + out
            if cid in self._consults:
                return (merges.REVIEW_RUNNING_REFUSAL,) + out
            helper = (getattr(self, "_merge_helpers", None) or {}).get(cid)
            if (helper and time.time() - float(helper.get("since") or 0.0)
                    <= dispatch.DISPATCH_BIND_WINDOW) \
                    or (path and self._session_inside(path)):
                return (merges.SESSION_INSIDE_REFUSAL,) + out
            if cid in (getattr(self, "_worktree_release_queue", None) or []) \
                    or cid in (getattr(self, "_worktree_releasing", None)
                               or set()):
                return (merges.RELEASE_PENDING_REFUSAL,) + out
        refusal = self._merging_refusal(cid, root)
        if refusal:
            return (refusal,) + out
        if expected_tip:
            ok, tip, _why = self._git_blocking(
                merges.argv_tip(root, f"refs/heads/{branch}"), root)
            if not ok or tip.decode("utf-8", "replace").strip() != expected_tip:
                return (merges.TIP_CHANGED_REFUSAL,) + out
        return ("",) + out

    def _merge_trunk_sync(self, root: str) -> str:
        """The local trunk's name: `origin/HEAD`'s branch when it exists
        locally, else `main` when that does, else `""`. **Executor.**"""
        ok, out, _why = self._git_blocking(worktrees.argv_base_ref(root), root)
        named = merges.trunk_from(out) if ok else ""
        for candidate in (named, merges.TRUNK_DEFAULT):
            if not candidate:
                continue
            found, _o, _w = self._git_blocking(
                worktrees.argv_branch_exists(root, candidate), root)
            if found:
                return candidate
        return ""

    async def _merge_trunk(self, root: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._merge_trunk_sync, root)

    async def _merge_tip(self, root: str, ref: str) -> str:
        """The commit `ref` names in `root`, or `""`."""
        ok, out, _why = await self._run_git(merges.argv_tip(root, ref), root)
        return out.decode("utf-8", "replace").strip() if ok else ""

    async def _merge_markers(self, path: str, names) -> list:
        """Which of git's in-progress markers exist for the work tree at
        `path`. A failed read answers every name (fail closed)."""
        ok, out, _why = await self._run_git(
            merges.argv_git_path(path, *names), path)
        if not ok:
            return list(names)
        lines = out.decode("utf-8", "replace").splitlines()
        loop = asyncio.get_running_loop()

        def present() -> list:
            found = []
            for name, line in zip(names, lines):
                where = line if os.path.isabs(line) else os.path.join(path, line)
                if os.path.lexists(where):
                    found.append(name)
            return found
        return await loop.run_in_executor(None, present)

    async def _merge_back_to_branch(self, path: str, branch: str) -> bool:
        """Put the card's folder back on its branch after any outcome but a
        landed merge — the merge commit, if one was made, is dropped (the
        next press rebuilds it) — and put the carried pack files back as
        they were (`_merge_recarry`). One door for every failure leg, or the
        Fix helper would open on a detached folder. False when git refused."""
        ok, _o, why = await self._run_git(
            merges.argv_checkout_branch(path, branch), path,
            timeout=worktrees.WORKTREE_ADD_TIMEOUT_SECONDS)
        if not ok:
            logger.info("could not put %s back on %s (%s)", path, branch, why)
            return False
        await self._merge_recarry(path)
        return True

    async def _merge_abandon(self, card_id: str, path: str, branch: str,
                             state: str, note: str) -> None:
        """A stop: the folder goes back on its branch and the stop is
        recorded. When the folder cannot be put back it says so, as
        `blocked` — never `conflict` / `checks_failed`, which offer Fix, and
        an assistant opened on a detached folder commits onto nothing."""
        if await self._merge_back_to_branch(path, branch):
            await self._merge_stop(card_id, state, note)
        else:
            await self._merge_stop(card_id, "blocked",
                                   merges.DETACHED_NOTE.format(path))

    def _root_of_folder(self, path: str) -> str:
        """The project root a card folder (`<root>/.worktrees/card-<id8>`)
        belongs to."""
        return os.path.dirname(os.path.dirname(str(path).rstrip(os.sep)))

    async def _merge_carried(self, path: str) -> list:
        """The carry manifest's rows for this folder (`[]` when none)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._read_pack_manifest, self._root_of_folder(path),
            self._pack_card_id(path))

    def _merge_uncarry_sync(self, root: str, path: str) -> str:
        """Take **every** carried pack file out of the folder before the
        merge, so nothing in its index can refuse the detach or the merge.
        Executor. `""` when done; the refusal words when a carried copy was
        edited in the folder (nothing is touched then) or a mark would not
        clear.

        A carried new file is an intent-to-add, skip-worktree entry and a
        carried tracked one is skip-worktree over the folder's own bytes; git
        refuses `checkout --detach` and `merge` over either (`Entry … not
        uptodate`, `local changes would be overwritten`, `untracked …
        would be overwritten` when the trunk now tracks the path), even with
        identical bytes. One path at a time and only for paths actually in
        the index: tracked and deleted rows go back to the folder's `HEAD`
        bytes, new copies whose bytes still match the manifest are deleted
        (an edited copy refuses, `DIRTY_FOLDER_REFUSAL`), and the manifest
        goes. The carry is applied again by the existing routine
        (`_sync_pack_copies`) once the folder is back on its branch. A
        deletion is never staged for a path `HEAD` tracks."""
        card_id = self._pack_card_id(path)
        rows = self._read_pack_manifest(root, card_id)
        if not rows:
            return ""
        rels = [r["path"] for r in rows]
        wt_real = os.path.realpath(path)
        # Pass one, touching nothing: an edited copy is the person's.
        for row in rows:
            dest = os.path.join(path, row["path"])
            parent = os.path.realpath(os.path.dirname(dest))
            if not (parent == wt_real or parent.startswith(wt_real + os.sep)):
                return merges.DIRTY_FOLDER_REFUSAL
            now = self._pack_file_digest(dest)
            if now is None:
                return merges.DIRTY_FOLDER_REFUSAL
            if row["kind"] == "deleted":
                if now != "":
                    return merges.DIRTY_FOLDER_REFUSAL
            elif now not in ("", row["sha256"]):
                return merges.DIRTY_FOLDER_REFUSAL
        ok, out, _why = self._git_blocking(
            merges.argv_ls_files_tagged(path, rels), path)
        if not ok:
            return merges.GIT_FAILED_REFUSAL.format(work_record.GIT_FAILED_REASON)
        indexed = merges.parse_tagged(out)
        in_head = set()
        for rel in rels:
            ok, tree, _why = self._git_blocking(worktrees.argv_ls_tree(path, rel),
                                                path)
            if not ok:
                return merges.GIT_FAILED_REFUSAL.format(
                    work_record.GIT_FAILED_REASON)
            if worktrees.parse_tree_mode(tree):
                in_head.add(rel)
        marked = [r for r in rels if "S" in indexed.get(r, ())]
        if marked:
            ok, _o, _why = self._git_blocking(
                worktrees.argv_skip_worktree(path, marked, False), path)
            if not ok:
                return merges.GIT_FAILED_REFUSAL.format(
                    "a carried file could not be released")
        added = [r for r in rels if r in indexed and r not in in_head]
        if added:
            self._git_blocking(worktrees.argv_rm_cached(path, added), path)
        for row in rows:
            rel = row["path"]
            if rel in in_head:
                continue
            try:
                os.unlink(os.path.join(path, rel))
            except OSError:
                pass  # already gone, or not a plain file: nothing to remove
        back = [r for r in rels if r in in_head]
        if back:
            ok, _o, _why = self._git_blocking(
                merges.argv_restore_head(path, back), path)
            if not ok:
                return merges.GIT_FAILED_REFUSAL.format(
                    "a carried file could not be put back")
        ok, out, _why = self._git_blocking(
            merges.argv_ls_files_tagged(path, rels), path)
        if not ok or any("S" in tags for tags in merges.parse_tagged(out).values()):
            return merges.GIT_FAILED_REFUSAL.format(
                "a carried file is still marked")
        try:
            os.unlink(worktrees.pack_manifest_path(root, card_id))
        except OSError:
            pass  # no manifest left to remove
        return ""

    async def _merge_uncarry(self, path: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._merge_uncarry_sync, self._root_of_folder(path), path)

    async def _merge_recarry(self, path: str) -> None:
        """The carry again, by the routine Start uses (`_sync_pack_copies`):
        the main checkout's uncommitted pack update is copied into the
        folder, marked and recorded. Run on every way back to the branch."""
        loop = asyncio.get_running_loop()
        note = await loop.run_in_executor(
            None, self._sync_pack_copies, self._root_of_folder(path), path,
            self._pack_card_id(path))
        if note:
            logger.info("merge: %s", note)

    def _pop_merging(self, card_id: str, token: str) -> None:
        """Replace, never mutate: the executor reads the map. Only the entry
        carrying `token` goes."""
        current = getattr(self, "_merging", None) or {}
        if (current.get(card_id) or {}).get("token") == token:
            self._merging = {k: v for k, v in current.items()
                             if k != card_id}

    async def merge_card(self, card_id: str, expected_tip=None) -> tuple:
        """The MERGE press. `(ok, detail)` — answers at once with
        `merges.MERGING_NOTE` and works in the background (the panel's POST
        times out at five seconds).

        The person's own verb, not an agent run: a confirmed press, with
        the gate (`_merge_gate_sync`) taken here and the folder, the main
        checkout and the trunk's tip re-checked by the task at the moment
        each is touched. `expected_tip` is the phone's current-state echo
        (absent: no guard)."""
        if self._board is None:
            return False, merges.BOARD_CLOSED_REFUSAL
        cid = str(card_id or "")
        # `None` is no guard (the Mac's plain press). A value that is present
        # is a guard and must be a whole commit hash: an empty or short one
        # is never read as "absent".
        if expected_tip is not None and not merges.is_tip(expected_tip):
            return False, merges.TIP_CHANGED_REFUSAL
        card = await self._board_call("get", cid)
        loop = asyncio.get_running_loop()
        refusal, root, _branch, _path = await loop.run_in_executor(
            None, functools.partial(
                self._merge_gate_sync, card,
                expected_tip=str(expected_tip or "")))
        if refusal:
            return False, refusal
        # No await between this look and the record below: of two presses
        # arriving together, one finds the other's entry.
        refusal = self._merging_refusal(cid, root)
        if refusal:
            return False, refusal
        token = secrets.token_hex(8)
        merging = dict(getattr(self, "_merging", None) or {})
        merging[cid] = {"root": root, "since": time.time(), "token": token}
        self._merging = merging
        task = asyncio.ensure_future(self._merge_card_task(
            cid, root, token, str(expected_tip or "")))
        self._merge_tasks.add(task)
        task.add_done_callback(self._merge_tasks.discard)
        await self._board_call("record_merge", cid, "", "")
        await self._publish_board()
        return True, merges.MERGING_NOTE

    async def _merge_card_task(self, card_id: str, root: str,
                               token: str, expected_tip: str = "") -> None:
        """The detached merge. Loop; never raises. `finally` pops the
        `_merging` entry it made and publishes."""
        try:
            await self._merge_run(card_id, root, expected_tip)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("the merge of card %s failed", card_id[:8],
                           exc_info=True)
            try:
                await self._merge_stop(
                    card_id, "blocked",
                    merges.GIT_FAILED_REFUSAL.format("an unexpected error"))
            except Exception:
                logger.debug("could not record the failed merge",
                             exc_info=True)
        finally:
            self._pop_merging(card_id, token)
            try:
                await self._publish_board()
            except Exception:
                logger.debug("could not publish after a merge", exc_info=True)

    async def _merge_stop(self, card_id: str, state: str, note: str) -> None:
        """A merge that did not land: record why, log one line (no path)."""
        await self._board_call("record_merge", card_id, state, note)
        card = await self._board_call("get", card_id)
        if card is not None:
            self._log_card_event(card, "card_merge_blocked", state=state)
        logger.info("merge of card %s stopped (%s): %s", card_id[:8], state,
                    note[:200])

    async def _merge_run(self, card_id: str, root: str,
                          expected_tip: str = "") -> None:
        """Steps 5 to 7 of the contract: the folder, the merge commit, the
        checks, the move of the trunk, the cleanup. Loop (git hops to the
        executor). `expected_tip`, when the press carried one, is checked
        again against the fresh branch tip before anything is touched."""
        cid = str(card_id)
        loop = asyncio.get_running_loop()
        card = await self._board_call("get", cid)
        if card is None:
            return
        branch = str(card.get("worktree_branch") or "")
        found, _o, why = await self._run_git(
            worktrees.argv_branch_exists(root, branch), root)
        if not branch or not found:
            await self._merge_stop(cid, "blocked",
                                   merges.NO_BRANCH_REFUSAL)
            return
        # The folder: the recorded one when it is still one, else made again
        # exactly as Start makes it (the setup script runs again).
        path = await self._reusable_worktree(card)
        if not path:
            path, _b, error = await self._prepare_worktree(card, root)
            if error:
                await self._merge_stop(
                    cid, "blocked", merges.FOLDER_FAILED_REFUSAL.format(error))
                return
            recorded, _why = await self._board_call(
                "record_worktree", cid, path, branch) or (None, "")
            if recorded is None:
                await self._merge_stop(cid, "blocked",
                                       merges.NO_CARD_REFUSAL)
                return
        if await self._merge_markers(path, merges.MID_MERGE_MARKERS):
            await self._merge_stop(cid, "blocked", merges.MID_MERGE_REFUSAL)
            return
        ok, status, _why = await self._run_git(merges.argv_status(path), path)
        if not ok or status:
            await self._merge_stop(cid, "blocked", merges.DIRTY_FOLDER_REFUSAL)
            return
        trunk = await self._merge_trunk(root)
        if not trunk:
            await self._merge_stop(cid, "blocked", merges.NO_TRUNK_REFUSAL)
            return
        base_tip = await self._merge_tip(root, f"refs/heads/{trunk}")
        branch_tip = await self._merge_tip(root, f"refs/heads/{branch}")
        if not base_tip or not branch_tip:
            await self._merge_stop(
                cid, "blocked", merges.GIT_FAILED_REFUSAL.format(
                    work_record.GIT_FAILED_REASON))
            return
        # The phone's echo, again at the moment the branch is first touched:
        # the press checked it, but the folder may have been made again and a
        # commit may have landed since.
        if expected_tip and branch_tip != expected_tip:
            await self._merge_stop(cid, "blocked", merges.TIP_CHANGED_REFUSAL)
            return
        contained, _o, _w = await self._run_git(
            worktrees.argv_is_ancestor(root, branch_tip, base_tip), root)
        if contained:
            await self._merge_land(card, root, path, branch, trunk,
                                   merges.ALREADY_MERGED_NOTE,
                                   branch_tip=branch_tip)
            return
        # The merge commit, built detached at the trunk's tip in the card's
        # own folder: the main checkout is never mid-anything and never
        # holds a merge commit it did not fast-forward to.
        refusal = await self._merge_uncarry(path)
        if refusal:
            await self._merge_abandon(cid, path, branch, "blocked", refusal)
            return
        ok, _o, why = await self._run_git(
            merges.argv_checkout_detach(path, base_tip), path,
            timeout=worktrees.WORKTREE_ADD_TIMEOUT_SECONDS)
        if not ok:
            await self._merge_abandon(cid, path, branch, "blocked",
                                      merges.GIT_FAILED_REFUSAL.format(why))
            return
        ok, _o, why = await self._run_git(
            merges.argv_merge(path, branch_tip, merges.merge_subject(card)),
            path, timeout=merges.MERGE_TIMEOUT_SECONDS,
            env=merges.merge_env())
        if not ok:
            _r, listing, _w = await self._run_git(
                merges.argv_unmerged(path), path)
            files = merges.parse_paths_z(listing)
            await self._run_git(merges.argv_merge_abort(path), path)
            if files:
                await self._merge_abandon(
                    cid, path, branch, "conflict",
                    merges.conflict_note(trunk, files))
            else:
                await self._merge_abandon(
                    cid, path, branch, "blocked",
                    merges.MERGE_FAILED_REFUSAL.format(why, trunk))
            return
        merge_tip = await self._merge_tip(path, "HEAD")
        if not merge_tip:
            await self._merge_abandon(
                cid, path, branch, "blocked", merges.GIT_FAILED_REFUSAL.format(
                    work_record.GIT_FAILED_REASON))
            return
        # The project's own checks, in the folder, at the merge commit.
        unchecked = False
        verdict = await loop.run_in_executor(
            None, self._setup_script_file_check, root,
            merges.MERGE_CHECK_SCRIPT)
        if verdict is None:
            unchecked = True
        else:
            refusal = await self._setup_script_refusal(
                root, str(card.get("tool") or ""),
                rel=merges.MERGE_CHECK_SCRIPT)
            if refusal:
                await self._merge_abandon(cid, path, branch, "blocked", refusal)
                return
            log_path = merges.merge_log_path(root, cid)
            refusal, failure, log = await loop.run_in_executor(
                None, functools.partial(
                    self._exec_setup_script, root, path, cid, branch,
                    rel=merges.MERGE_CHECK_SCRIPT, log_path=log_path,
                    timeout=merges.MERGE_CHECK_TIMEOUT_SECONDS))
            if refusal:
                await self._merge_abandon(cid, path, branch, "blocked", refusal)
                return
            if failure:
                await self._merge_abandon(
                    cid, path, branch, "checks_failed",
                    merges.CHECKS_FAILED_NOTE.format(failure, log))
                return
            ok, status, _why = await self._run_git(
                merges.argv_status(path), path)
            if not ok or status:
                await self._merge_abandon(cid, path, branch, "blocked",
                                          merges.DIRTY_FOLDER_REFUSAL)
                return
        # The card may have been dragged out of Done, its check reopened or
        # its branch moved while the checks ran: the move of the trunk is
        # destructive, so the card is judged again at the moment it fires.
        fresh = await self._board_call("get", cid)
        refusal = await loop.run_in_executor(
            None, self._merge_recheck_sync, fresh, branch_tip)
        if refusal:
            await self._merge_abandon(cid, path, branch, "blocked", refusal)
            return
        refusal = await self._merge_checkout_gate(root, trunk, base_tip,
                                                  merge_tip)
        if refusal:
            await self._merge_abandon(cid, path, branch, "blocked", refusal)
            return
        await self._merge_land(card, root, path, branch, trunk,
                               merges.MERGED_NOTE.format(trunk, merge_tip[:8]),
                               unchecked=unchecked, branch_tip=branch_tip)

    def _merge_recheck_sync(self, card, branch_tip: str) -> str:
        """The refusal words, or `""`: the gate's card-side rungs read afresh
        from inside the running task (Done, a branch, the hand-check still
        settled, the project still watched) and the branch still at the tip
        the merge commit was built from. The busy rungs are the task's own
        and left out. **Executor.**"""
        if not isinstance(card, dict) or not card:
            return merges.NO_CARD_REFUSAL
        if str(card.get("column_name") or "") != "done":
            return merges.NOT_DONE_REFUSAL
        branch = str(card.get("worktree_branch") or "")
        if not branch:
            return merges.NO_BRANCH_REFUSAL
        refusal = self._merge_manual_refusal(card)
        if refusal:
            return refusal
        root = dispatch.normalise_root(str(card.get("root") or ""))
        if not root or not enrollment.root_enrolled(root):
            return merges.NOT_ENROLLED_REFUSAL
        ok, tip, _why = self._git_blocking(
            merges.argv_tip(root, f"refs/heads/{branch}"), root)
        if not ok or tip.decode("utf-8", "replace").strip() != branch_tip:
            return merges.TIP_CHANGED_REFUSAL
        return ""

    async def _merge_checkout_gate(self, root: str, trunk: str,
                                   base_tip: str, merge_tip: str) -> str:
        """The last look at the main checkout, then the move. `""` when the
        trunk moved forward onto `merge_tip`; the refusal words otherwise,
        with **nothing changed**.

        (a) the trunk still at the tip the merge was built on; (b) the main
        checkout in the middle of nothing; (c) the trunk not checked out in
        some other folder; (d) the trunk checked out in the main checkout:
        its uncommitted files must not meet the files the merge changes,
        then `--ff-only`; (e) checked out nowhere: only the pointer moves,
        and only from the old value."""
        if await self._merge_tip(root, f"refs/heads/{trunk}") != base_tip:
            return merges.TRUNK_MOVED_REFUSAL.format(trunk)
        if await self._merge_markers(root, merges.ROOT_BUSY_MARKERS):
            return merges.ROOT_BUSY_REFUSAL
        ok, out, why = await self._run_git(
            worktrees.argv_worktree_list(root), root)
        if not ok:
            return merges.GIT_FAILED_REFUSAL.format(why)
        loop = asyncio.get_running_loop()
        real_root = await loop.run_in_executor(None, os.path.realpath, root)
        listed = merges.parse_worktree_branches(out.decode("utf-8", "replace"))
        for where, held in listed.items():
            if held == trunk and where != real_root:
                return merges.TRUNK_ELSEWHERE_REFUSAL.format(trunk, where)
        # Two independent answers to "what is the main checkout on": the
        # worktree list and `symbolic-ref`. Both must be readable and agree
        # (a detached HEAD is `""` in the list and a failed `symbolic-ref`);
        # otherwise nothing moves — a guess here would pick `update-ref`
        # under a checkout that is really on the trunk.
        if real_root not in listed:
            return merges.ROOT_UNSURE_REFUSAL
        listed_branch = listed[real_root]
        ok, head, _why = await self._run_git(merges.argv_head_branch(root),
                                             root)
        if ok:
            if head.decode("utf-8", "replace").strip() != listed_branch:
                return merges.ROOT_UNSURE_REFUSAL
        elif listed_branch:
            return merges.ROOT_UNSURE_REFUSAL
        on_trunk = listed_branch == trunk
        if on_trunk:
            ok, listing, why = await self._run_git(merges.argv_status(root),
                                                   root)
            if not ok:
                return merges.GIT_FAILED_REFUSAL.format(why)
            ok, incoming, why = await self._run_git(
                merges.argv_incoming(root, base_tip, merge_tip), root)
            if not ok:
                return merges.GIT_FAILED_REFUSAL.format(why)
            clash = merges.overlap(merges.parse_status_paths(listing),
                                   merges.parse_paths_z(incoming))
            if clash:
                return merges.OVERLAP_REFUSAL.format(merges.listed(clash))
            ok, _o, why = await self._run_git(
                merges.argv_ff(root, merge_tip), root,
                timeout=merges.FF_TIMEOUT_SECONDS, env=merges.merge_env())
            if not ok:
                return merges.FF_FAILED_REFUSAL.format(trunk, why)
            return ""
        ok, _o, why = await self._run_git(
            merges.argv_update_ref(root, trunk, merge_tip, base_tip), root)
        if not ok:
            return merges.TRUNK_MOVED_REFUSAL.format(trunk)
        return ""

    async def _merge_delete_branch(self, root: str, branch: str,
                                   branch_tip: str, trunk: str) -> bool:
        """Delete the landed branch's ref, guarded by the tip that was
        merged (`update-ref -d <ref> <old>`; never `branch -D`). `branch -d`
        judges a branch against whatever the main checkout has checked out,
        so it refused whenever that was not the trunk. Kept (False) when the
        tip is not part of the trunk after all, when the main checkout or
        another folder has the branch checked out, or when the ref moved."""
        contained, _o, _w = await self._run_git(
            worktrees.argv_is_ancestor(root, branch_tip, f"refs/heads/{trunk}"),
            root)
        if not contained:
            return False
        ok, out, _why = await self._run_git(
            worktrees.argv_worktree_list(root), root)
        if not ok:
            return False
        if branch in merges.parse_worktree_branches(
                out.decode("utf-8", "replace")).values():
            return False
        ok, _o, why = await self._run_git(
            merges.argv_branch_delete_ref(root, branch, branch_tip), root)
        if not ok:
            logger.info("could not delete %s (%s)", branch, why)
        return ok

    async def _merge_land(self, card: dict, root: str, path: str,
                          branch: str, trunk: str, note: str, *,
                          unchecked: bool = False,
                          branch_tip: str = "") -> None:
        """After the trunk holds the branch: remove the folder through the
        release's own gates (no `--force`), delete the branch (guarded by
        the tip merged), clear the card's memory of both, and say so. A
        folder the release kept is put back on its branch and keeps it."""
        cid = str(card.get("id") or "")
        await self._maybe_release_worktree({"id": cid}, merging=True)
        after = await self._board_call("get", cid) or {}
        folder_kept = bool(str(after.get("worktree_path") or ""))
        branch_kept = folder_kept
        if folder_kept:
            await self._merge_back_to_branch(
                str(after.get("worktree_path") or path), branch)
        else:
            tip = branch_tip or await self._merge_tip(
                root, f"refs/heads/{branch}")
            if tip and await self._merge_delete_branch(root, branch, tip,
                                                       trunk):
                await self._board_call("clear_worktree", cid, branch=True)
            else:
                branch_kept = True
        line = note
        if unchecked:
            line += merges.MERGED_UNCHECKED_NOTE
        if folder_kept:
            line += merges.FOLDER_KEPT_SUFFIX
        if branch_kept:
            line += merges.BRANCH_KEPT_SUFFIX
        await self._board_call("record_merge", cid, "merged", line)
        done = await self._board_call("get", cid)
        if done is not None:
            self._log_card_event(done, "card_merged", trunk=trunk)
        logger.info("card %s: merged %s into %s", cid[:8], branch, trunk)

    # -- Fix and Run review: an assistant in the card's folder --

    async def _merge_folder(self, card: dict, root: str) -> tuple:
        """`(path, error)` — the card's folder for a helper: the recorded
        one while it is still one, else made again as Start makes it and
        recorded. Loop; the caller holds `_dispatch_lock`."""
        cid = str(card.get("id") or "")
        path = await self._reusable_worktree(card)
        if path:
            return path, ""
        found, _o, _w = await self._run_git(
            worktrees.argv_branch_exists(
                root, str(card.get("worktree_branch") or "")), root)
        if not found:
            return "", merges.NO_BRANCH_REFUSAL
        path, branch, error = await self._prepare_worktree(card, root)
        if error:
            return "", merges.FOLDER_FAILED_REFUSAL.format(error)
        recorded, _why = await self._board_call(
            "record_worktree", cid, path, branch) or (None, "")
        if recorded is None:
            return "", merges.NO_CARD_REFUSAL
        return path, ""

    def _hold_folder(self, card_id: str, root: str, project: str,
                     kind: str) -> None:
        """Mark the card's folder as spoken for before it is recreated, so
        the release cannot remove it between the record and the spawn.
        Replaced, never mutated."""
        held = dict(getattr(self, "_merge_helpers", None) or {})
        held[str(card_id)] = {"root": str(root), "project": str(project),
                              "since": time.time(), "kind": kind}
        self._merge_helpers = held

    def _drop_hold(self, card_id: str) -> None:
        held = getattr(self, "_merge_helpers", None) or {}
        if str(card_id) in held:
            self._merge_helpers = {k: v for k, v in held.items()
                                   if k != str(card_id)}

    async def _helper_gate(self, card_id: str, *, fix: bool) -> tuple:
        """`(refusal, card, root, branch, path)` for Fix and Run review:
        board open, the gate, the dispatch switch. Not under the lock yet."""
        if self._board is None:
            return merges.BOARD_CLOSED_REFUSAL, None, "", "", ""
        card = await self._board_call("get", str(card_id or ""))
        loop = asyncio.get_running_loop()
        refusal, root, branch, path = await loop.run_in_executor(
            None, functools.partial(self._merge_gate_sync, card))
        if refusal:
            return refusal, card, root, branch, path
        if fix and str(card.get("merge_state") or "") \
                not in merges.FIX_STATES:
            return merges.FIX_NOT_NEEDED_REFUSAL, card, root, branch, path
        if not self.board_dispatch_enabled:
            return merges.DISPATCH_OFF_REFUSAL, card, root, branch, path
        return "", card, root, branch, path

    async def fix_merge_card(self, card_id: str) -> tuple:
        """The Fix press: start the card's own assistant in the card's
        folder to bring the main line into the card branch and repair it
        there. `(ok, detail)`. Behind `board_dispatch`, the gate, and
        `_dispatch_lock` — a launch like any other."""
        refusal, card, _root, _branch, _path = await self._helper_gate(
            card_id, fix=True)
        if refusal:
            return False, refusal
        async with self._dispatch_lock:
            return await self._spawn_merge_fix_locked(str(card_id))

    async def _spawn_merge_fix_locked(self, card_id: str) -> tuple:
        # Everything is read afresh under the lock: the first look was a
        # moment ago and a launch spends a terminal.
        refusal, card, root, branch, _path = await self._helper_gate(
            card_id, fix=True)
        if refusal:
            return False, refusal
        cid = str(card.get("id") or "")
        tool = str(card.get("tool") or "claude")
        loop = asyncio.get_running_loop()
        self._hold_folder(cid, root, str(card.get("project") or ""), "fix")
        spawned = False
        try:
            path, error = await self._merge_folder(card, root)
            if error:
                return False, error
            trunk = await self._merge_trunk(root)
            if not trunk:
                return False, merges.NO_TRUNK_REFUSAL
            # The helper brings the main line into the card branch in this
            # folder, which the carried pack files would refuse: release them
            # first (an edited one refuses the press, in words).
            refusal = await self._merge_uncarry(path)
            if refusal:
                return False, refusal
            state = str(card.get("merge_state") or "")
            prompt = dispatch.merge_fix_prompt(
                card, branch=branch, worktree=path, trunk=trunk, state=state,
                detail=str(card.get("merge_note") or ""),
                log=(merges.merge_log_path(root, cid)
                     if state == "checks_failed" else ""))
            roots = await loop.run_in_executor(None, self._known_project_roots)
            in_flight = [c for c in self._launch_inflight(
                await self._board_call("cards") or [])
                if c.get("id") != f"merge-fix:{cid}"]
            ok, detail = dispatch.helper_guard(
                card, roots=roots, in_flight=in_flight, now=time.time(),
                last_attempt=self._merge_attempts.get(cid), key="merge-fix",
                tool=tool, prompt=prompt)
            if not ok:
                return False, detail
            executable = await loop.run_in_executor(
                None, dispatch.resolve_executable, tool)
            if not executable:
                return False, merges.HELPER_TOOL_MISSING_REFUSAL.format(tool)
            argv = dispatch.argv_for(
                tool, executable, prompt,
                model=self._agent_model_for(root, tool, "main"))
            name = ("fix: " + (card.get("title") or "card"))[:40]
            spawner = (dispatch.spawn_local
                       if self._uses_own_terminal(cid, None)
                       else dispatch.spawn)
            where = {"cwd": path} if path and path != root else {}
            ok, spawn_detail, _pid = await spawner(
                root, argv, name, stamp=origin.stamp("card-merge-fix", cid),
                **where)
            if not ok:
                return False, spawn_detail
            spawned = True
            self._hold_folder(cid, root, str(card.get("project") or ""),
                              "fix")
            self._merge_attempts[cid] = time.time()
            await self._publish_board()
            logger.info("card %s: started %s in %s to fix its merge",
                        cid[:8], tool, path)
            return True, spawn_detail
        finally:
            if not spawned:
                self._drop_hold(cid)

    async def run_card_review(self, card_id: str) -> tuple:
        """The Run review press: an assistant reviews the card's branch and
        answers onto the card's thread, where the first line is read.
        `(ok, detail)`. Claude on every card — only Claude's channel has
        `dark_army_answer_card`."""
        refusal, _card, _root, _branch, _path = await self._helper_gate(
            card_id, fix=False)
        if refusal:
            return False, refusal
        async with self._dispatch_lock:
            return await self._spawn_card_review_locked(str(card_id))

    async def _spawn_card_review_locked(self, card_id: str) -> tuple:
        refusal, card, root, branch, _path = await self._helper_gate(
            card_id, fix=False)
        if refusal:
            return False, refusal
        cid = str(card.get("id") or "")
        loop = asyncio.get_running_loop()
        self._hold_folder(cid, root, str(card.get("project") or ""), "review")
        spawned = False
        try:
            path, error = await self._merge_folder(card, root)
            if error:
                return False, error
            trunk = await self._merge_trunk(root)
            if not trunk:
                return False, merges.NO_TRUNK_REFUSAL
            tip = await self._merge_tip(root, f"refs/heads/{branch}")
            if not tip:
                return False, merges.NO_BRANCH_REFUSAL
            prompt = dispatch.review_prompt(card, branch=branch, trunk=trunk,
                                            worktree=path)
            roots = await loop.run_in_executor(None, self._known_project_roots)
            in_flight = self._launch_inflight(
                await self._board_call("cards") or [])
            ok, detail = dispatch.helper_guard(
                card, roots=roots, in_flight=in_flight, now=time.time(),
                last_attempt=self._review_attempts.get(cid), key="consult",
                tool="claude", prompt=prompt)
            if not ok:
                return False, detail
            executable = await loop.run_in_executor(
                None, dispatch.resolve_executable, "claude")
            if not executable:
                return False, dispatch.NOT_INSTALLED_REFUSAL.format(
                    tool="claude")
            argv = dispatch.argv_for(
                "claude", executable, prompt,
                model=self._agent_model_for(root, "claude", "main"))
            name = ("review: " + (card.get("title") or "card"))[:40]
            baseline = self._live_session_ids()
            spawner = (dispatch.spawn_local
                       if self._uses_own_terminal(cid, None)
                       else dispatch.spawn)
            where = {"cwd": path} if path and path != root else {}
            ok, spawn_detail, shell_pid = await spawner(
                root, argv, name, stamp=origin.stamp("card-review", cid),
                **where)
            if not ok:
                return False, spawn_detail
            spawned = True
            now = time.time()
            self._review_attempts[cid] = now
            self._consults[cid] = {
                "card_id": cid,
                "project": card.get("project") or "",
                "root": root,
                "tool": "claude",
                "baseline": baseline,
                "started": now,
                "session_id": "",
                "shell_pid": shell_pid,
                "purpose": "review",
                "tip": tip,
            }
            await self._publish_board()
            logger.info("card %s: started a review of %s", cid[:8], branch)
            return True, spawn_detail
        finally:
            if not spawned:
                self._drop_hold(cid)

    async def _record_review_from_answer(self, card_id: str, entry: dict,
                                         text: str) -> None:
        """A retired review consult's answer: read the verdict off its first
        line and remember it with the branch version it judged. No
        `VERDICT:` line leaves the columns untouched (the message stays on
        the thread)."""
        verdict = merges.parse_verdict(text)
        if not verdict:
            return
        tip = str((entry or {}).get("tip") or "")
        await self._board_call("record_review_verdict", card_id, verdict, tip)
        card = await self._board_call("get", card_id)
        if card is not None:
            self._log_card_event(card, "card_review_verdict", verdict=verdict)

    # -- The card says so --

    def _review_running(self, card_id: str) -> bool:
        entry = self._consults.get(str(card_id))
        return bool(entry and entry.get("purpose") == "review")

    def _merge_line(self, card: dict) -> str:
        """The sentence a card draws about its merge, or `""`. **Executor.**
        Composed here and drawn verbatim by both clients: the running words
        while merging, else the stored note — with, for a state the Fix
        press answers, whether an assistant is already at work in the
        folder."""
        cid = str(card.get("id") or "")
        if cid in (getattr(self, "_merging", None) or {}):
            return merges.MERGING_NOTE
        state = str(card.get("merge_state") or "")
        note = str(card.get("merge_note") or "")
        if state not in merges.STATES or not state or not note:
            return ""
        if state in merges.FIX_STATES:
            path = str(card.get("worktree_path") or "")
            at_work = self._merge_hold(cid) or bool(
                path and self._session_inside(path))
            note += (merges.HELPER_AT_WORK_SUFFIX if at_work
                     else merges.PRESS_AGAIN_SUFFIX)
        return note

    def _decorate_merge(self, out: dict) -> None:
        """Publish the card's four review-and-merge keys where non-empty and
        take the stored bookkeeping off the copy: `merge_state` (with
        `merging` while the task runs), `merge_line`, `review_verdict` and
        `review_running`. `merge_note` and `review_tip` never ride."""
        cid = str(out.get("id") or "")
        stored = str(out.get("merge_state") or "")
        line = self._merge_line(out)
        out.pop("merge_note", None)
        out.pop("review_tip", None)
        if cid in (getattr(self, "_merging", None) or {}):
            out["merge_state"] = merges.SNAPSHOT_MERGING
        elif not stored:
            out.pop("merge_state", None)
        if line:
            out["merge_line"] = line
        if not str(out.get("review_verdict") or ""):
            out.pop("review_verdict", None)
        if self._review_running(cid):
            out["review_running"] = True
        # The one fact every surface draws MERGE, Fix and Run review from:
        # the gate without its busy rungs (Done, a branch, not merged, the
        # hand-check settled — a Failed one reads from the check file,
        # memoised by its mtime — enrolled, a checkout, not merging).
        # Published only on a Done card that has a branch; no git here.
        if str(out.get("column_name") or "") == "done" \
                and str(out.get("worktree_branch") or ""):
            try:
                refusal = self._merge_gate_sync(out, busy=False, cached=True)[0]
            except Exception:
                logger.debug("merge_offered could not be decided",
                             exc_info=True)
                refusal = "unknown"
            out["merge_offered"] = not refusal

    # -- Remembering the branch of a card finished before this existed --

    def _consider_standing_branch(self, card: dict) -> None:
        """Once per process per card: a Done card of a git project that
        names neither a folder nor a branch and has no merge state may have
        a branch its name implies. Queued for the loop to look. **Executor,
        no git** (a stat for the checkout, nothing else)."""
        card = card if isinstance(card, dict) else {}
        cid = str(card.get("id") or "")
        if not cid or str(card.get("column_name") or "") != "done":
            return
        if card.get("worktree_branch") or card.get("worktree_path") \
                or card.get("merge_state"):
            return
        if cid in self._branch_backfill_seen:
            return
        # Seen first: a project that is not a git checkout is asked once too.
        self._branch_backfill_seen = self._branch_backfill_seen | {cid}
        root = dispatch.normalise_root(str(card.get("root") or ""))
        if not root or not self._git_checkout(root):
            return
        self._branch_backfill_queue = self._branch_backfill_queue + [
            {"id": cid, "root": root, "title": str(card.get("title") or "")}]

    async def _flush_branch_backfill(self) -> None:
        """Look for the queued cards' branches as one detached task, one in
        flight. A branch that exists and is not yet part of the trunk is
        recorded on the card (no folder); one already contained reads
        merged; none leaves the card as it was."""
        if not self._branch_backfill_queue:
            return
        task = getattr(self, "_branch_backfill_task", None)
        if task is not None and not task.done():
            return
        queue, self._branch_backfill_queue = self._branch_backfill_queue, []
        try:
            self._branch_backfill_task = asyncio.ensure_future(
                self._backfill_branches(queue))
        except RuntimeError:
            self._branch_backfill_queue = queue

    async def _backfill_branches(self, queue: list) -> None:
        changed = False
        trunks: dict = {}
        for item in queue:
            try:
                cid, root = item["id"], item["root"]
                branch = worktrees.branch_name(
                    {"id": cid, "title": item.get("title")})
                found, _o, _w = await self._run_git(
                    worktrees.argv_branch_exists(root, branch), root)
                if not found:
                    continue
                if root not in trunks:
                    trunks[root] = await self._merge_trunk(root)
                trunk = trunks[root]
                merged = False
                if trunk:
                    merged, _o, _w = await self._run_git(
                        worktrees.argv_is_ancestor(root, branch, trunk), root)
                if merged:
                    result = await self._board_call(
                        "record_merge", cid, "merged",
                        merges.ALREADY_MERGED_NOTE)
                else:
                    result = await self._board_call(
                        "record_worktree", cid, "", branch)
                changed = changed or bool(result and result[0])
            except Exception:
                logger.debug("branch backfill failed for %s",
                             item.get("id"), exc_info=True)
        if changed:
            await self._publish_board()
        if self._branch_backfill_queue:
            await self._flush_branch_backfill()

    # -- The Changes read: on demand only, never the snapshot path --

    def _changes_empty(self, card_id: str, reason: str) -> dict:
        return {"available": False, "card_id": str(card_id), "branch": "",
                "trunk": "", "merge_base": "", "branch_tip": "", "ahead": 0,
                "behind": 0, "commits": [], "files": [], "files_total": 0,
                "files_truncated": False, "commits_truncated": False,
                "merge_offered": False, "merge_refusal": "",
                "merge_state": "", "merge_note": "",
                "review": {"verdict": "", "tip": "", "current": False},
                "generated_at": time.time(), "reason": reason}

    def _changes_context(self, card_id: str) -> tuple:
        """`(unavailable_body_or_None, context)`. **Executor**, under
        `_changes_lock`. The card is re-read, the root's enrolment and
        checkout re-checked, and the trunk, both tips and the merge base
        read — the git every page needs."""
        cid = str(card_id or "")
        if self._board is None:
            return self._changes_empty(cid, merges.BOARD_CLOSED_REFUSAL), {}
        card = self._board.get(cid)
        if card is None:
            return self._changes_empty(cid, merges.NO_CARD_REFUSAL), {}
        branch = str(card.get("worktree_branch") or "")
        if not branch:
            return self._changes_empty(cid, merges.NO_BRANCH_REFUSAL), {}
        root = dispatch.normalise_root(str(card.get("root") or ""))
        if not root or not enrollment.root_enrolled(root):
            return self._changes_empty(
                cid, merges.NOT_ENROLLED_REFUSAL), {}
        if not self._git_checkout(root):
            return self._changes_empty(
                cid, merges.NOT_A_CHECKOUT_REFUSAL), {}
        trunk = self._merge_trunk_sync(root)
        if not trunk:
            return self._changes_empty(cid, merges.NO_TRUNK_REFUSAL), {}
        tips = []
        for ref in (f"refs/heads/{trunk}", f"refs/heads/{branch}"):
            ok, out, why = self._git_blocking(merges.argv_tip(root, ref), root)
            tip = out.decode("utf-8", "replace").strip() if ok else ""
            if not tip:
                return self._changes_empty(
                    cid, merges.GIT_FAILED_REFUSAL.format(why)), {}
            tips.append(tip)
        base_tip, branch_tip = tips
        ok, out, why = self._git_blocking(
            merges.argv_merge_base(root, base_tip, branch_tip), root)
        merge_base = out.decode("utf-8", "replace").strip() if ok else ""
        if not merge_base:
            return self._changes_empty(
                cid, merges.GIT_FAILED_REFUSAL.format(why)), {}
        return None, {"card": card, "root": root, "branch": branch,
                      "trunk": trunk, "base_tip": base_tip,
                      "branch_tip": branch_tip, "merge_base": merge_base}

    def _changes_files(self, ctx: dict) -> tuple:
        """`(rows, truncated, total, reason)` for the branch against the
        merge base — the one listing both the page and a file's index use."""
        ok, out, why = self._git_blocking(
            merges.argv_numstat_range(ctx["root"], ctx["merge_base"],
                                      ctx["branch_tip"]), ctx["root"])
        if not ok:
            return [], False, 0, why or work_record.GIT_FAILED_REASON
        rows, truncated, total = merges.parse_numstat_z(out)
        return rows, truncated, total, ""

    def _card_changes_sync(self, card_id: str) -> dict:
        """The Changes page: commits, files with added/removed counts, what
        MERGE would say. **Executor.** Read-only git against the merge base,
        bounded, serialised by `_changes_lock`, and never part of any
        snapshot."""
        with self._changes_lock:
            body, ctx = self._changes_context(card_id)
            if body is not None:
                return body
            root = ctx["root"]
            card = ctx["card"]
            ok, out, _why = self._git_blocking(
                merges.argv_ahead_behind(root, ctx["base_tip"],
                                         ctx["branch_tip"]), root)
            behind, ahead = merges.parse_ahead_behind(out) if ok else (0, 0)
            ok, out, _why = self._git_blocking(
                merges.argv_log(root, ctx["merge_base"], ctx["branch_tip"]),
                root)
            commits, commits_cut = merges.parse_log(out) if ok else ([], False)
            files, files_cut, total, reason = self._changes_files(ctx)
            refusal, _r, _b, _p = self._merge_gate_sync(card, busy=False)
            verdict = str(card.get("review_verdict") or "")
            review_tip = str(card.get("review_tip") or "")
            return {
                "available": True, "card_id": str(card.get("id") or ""),
                "branch": ctx["branch"], "trunk": ctx["trunk"],
                "merge_base": merges.sha8(ctx["merge_base"]),
                "branch_tip": ctx["branch_tip"], "ahead": ahead,
                "behind": behind, "commits": commits, "files": files,
                "files_total": total, "files_truncated": files_cut,
                "commits_truncated": commits_cut,
                "merge_offered": not refusal, "merge_refusal": refusal,
                "merge_state": (merges.SNAPSHOT_MERGING
                                if str(card.get("id")) in
                                (getattr(self, "_merging", None) or {})
                                else str(card.get("merge_state") or "")),
                "merge_note": self._merge_line(card),
                "review": {"verdict": verdict,
                           "tip": merges.sha8(review_tip) if verdict else "",
                           "current": bool(verdict) and merges.review_current(
                               review_tip, ctx["branch_tip"])},
                "generated_at": time.time(), "reason": reason}

    def _card_change_diff_sync(self, card_id: str, index: int,
                               tip: str) -> tuple:
        """`(status, body)` for one file's changes. `tip` must be the branch
        tip the list was read at (`CHANGES_MOVED`, 409); `index` indexes the
        listing recomputed in the same hop; the path is contained in the
        real root (`workspace._contains`) though git produced it."""
        with self._changes_lock:
            body, ctx = self._changes_context(card_id)
            if body is not None:
                return 200, {"available": False, "path": "", "text": "",
                             "truncated": False,
                             "reason": body.get("reason", "")}
            if str(tip or "") != ctx["branch_tip"]:
                return 409, {"error": merges.CHANGES_MOVED_REFUSAL}
            rows, _cut, _total, reason = self._changes_files(ctx)
            if reason:
                return 200, {"available": False, "path": "", "text": "",
                             "truncated": False, "reason": reason}
            if not (0 <= int(index) < len(rows)):
                return 404, {"error": merges.NO_SUCH_FILE_REFUSAL}
            rel = str(rows[int(index)]["path"])
            real_root = os.path.realpath(ctx["root"])
            target = os.path.realpath(os.path.join(real_root, rel))
            if not workspace._contains(real_root, target):
                return 404, {"error": "that file is outside the card's own "
                                      "project"}
            ok, out, why = self._git_blocking(
                merges.argv_range_file_diff(
                    ctx["root"], ctx["merge_base"], ctx["branch_tip"], rel),
                ctx["root"], truncate=True)
            if not ok and not out:
                return 200, {"available": False, "path": rel, "text": "",
                             "truncated": False,
                             "reason": why or work_record.GIT_FAILED_REASON}
            text, truncated = work_record.clamp_diff(out)
            return 200, {"available": True, "path": rel, "text": text,
                         "truncated": truncated, "reason": ""}

    async def card_changes_report(self, card_id: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._card_changes_sync, str(card_id or ""))

    async def card_change_diff(self, card_id: str, index: int,
                               tip: str) -> tuple:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._card_change_diff_sync, str(card_id or ""),
            int(index), str(tip or ""))

    # --- The per-project parallel dial ---

    def set_board_parallel(self, n: int) -> None:
        """How many agents may work at once in one project. Clamps, and says so.

        The daemon's own refusal of an out-of-range value, and the only writer
        of the attribute the gate and the snapshot both read. A hand-edited
        `preferences.json` carrying 50 runs at `BOARD_PARALLEL_MAX`, and
        because the snapshot publishes the *clamped* number the panel's
        readout can never show a limit Dark Army is not actually obeying.

        Plain attribute write, like the chime: the gate reads it at the moment
        it decides, so lowering the dial holds the *next* card rather than
        unwinding a session already launched.
        """
        limit = self._clamp_parallel(n)
        self.board_parallel_limit = limit
        logger.info("Board parallelism set to %d agent(s) per project", limit)

    @staticmethod
    def _clamp_parallel(n) -> int:
        """One integer, brought into 1..4. The only bound in the app.

        Extracted from `set_board_parallel` so the per-project overrides get
        the identical refusal: a hand-edited `preferences.json` carrying 50
        runs at `BOARD_PARALLEL_MAX` whichever dial it was written on, and a
        second clamp app-side is the two-bounds-drift mistake.
        """
        try:
            want = int(n)
        except (TypeError, ValueError):
            want = _D.BOARD_PARALLEL_MIN
        limit = max(_D.BOARD_PARALLEL_MIN, min(_D.BOARD_PARALLEL_MAX, want))
        if limit != want:
            logger.info("board parallelism %s is out of range; using %d",
                        n, limit)
        return limit

    def set_board_parallel_override(self, root: str, n) -> None:
        """One project's own dial. Thread-safe by replacement.

        `n` of `None`, a non-int, or anything below 1 **removes** the entry —
        0 is never a legal limit, so it is the unambiguous "back to the
        machine default" on the wire. Everything else is clamped exactly as
        the machine dial is.

        Builds a new dict and assigns once: this runs on the panel reader
        thread while `_parallel_limit_for` reads on the executor, and an
        in-place write during an executor read is the torn read to refuse.
        """
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            logger.info("ignoring a per-project parallel dial with no project")
            return
        current = dict(self.board_parallel_overrides)
        if n is None:
            current.pop(key, None)
        else:
            try:
                want = int(n)
            except (TypeError, ValueError):
                want = 0
            if want < _D.BOARD_PARALLEL_MIN:
                current.pop(key, None)
            else:
                current[key] = self._clamp_parallel(want)
        self.board_parallel_overrides = current
        logger.info("Board parallelism for %s set to %s", key,
                    current.get(key, "the shared default"))

    def set_board_parallel_overrides(self, mapping) -> None:
        """The whole stored map at once — the startup feed. Same clamp, one
        assignment; an unusable entry is dropped rather than failing the lot."""
        built: dict = {}
        for root, value in (mapping or {}).items():
            key = dispatch.normalise_root(str(root or ""))
            if not key:
                continue
            try:
                want = int(value)
            except (TypeError, ValueError):
                continue
            if want < _D.BOARD_PARALLEL_MIN:
                continue
            built[key] = self._clamp_parallel(want)
        self.board_parallel_overrides = built
        if built:
            logger.info("Per-project board parallelism loaded for %d project(s)",
                        len(built))

    def _parallel_limit_for(self, root) -> int:
        """How many agents may work at once in *this* project.

        The one resolution seam. Every gate, drain, log line and published
        figure asks this rather than reading `board_parallel_limit`, so a
        future site cannot reintroduce the machine-wide rule by accident.
        """
        key = dispatch.normalise_root(str(root or ""))
        if key:
            override = self.board_parallel_overrides.get(key)
            if override is not None:
                return max(_D.BOARD_PARALLEL_MIN, int(override))
        return max(_D.BOARD_PARALLEL_MIN, int(self.board_parallel_limit or 0))

    # --- Card isolation, per project (docs/card-worktrees.md) ---

    def set_board_isolation_override(self, root: str, enabled) -> None:
        """One project's isolation switch. Thread-safe by replacement,
        `set_board_parallel_override`'s shape: `False` stores the key (the
        map holds only the projects that switched it off), `True` or `None`
        pops it — back to the default, which is on."""
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            logger.info("ignoring a per-project isolation switch with no project")
            return
        current = dict(getattr(self, "board_isolation_overrides", None) or {})
        if enabled is False:
            current[key] = False
        else:
            current.pop(key, None)
        self.board_isolation_overrides = current
        logger.info("Card isolation for %s is %s", key,
                    "off" if enabled is False else "on")

    def set_board_isolation_overrides(self, mapping) -> None:
        """The whole stored map at once — the startup feed. Only a real
        `False` is an entry; anything else is dropped rather than failing
        the lot. One assignment."""
        built: dict = {}
        if isinstance(mapping, dict):
            for root, value in mapping.items():
                key = dispatch.normalise_root(str(root or ""))
                if key and value is False:
                    built[key] = False
        self.board_isolation_overrides = built
        if built:
            logger.info("Card isolation is off for %d project(s)", len(built))

    def _isolation_for(self, root) -> bool:
        """Whether a Start in this project works in its own worktree. On
        unless the project's override says `False`. The one resolution
        seam; the git test is separate (`_git_checkout`)."""
        key = dispatch.normalise_root(str(root or ""))
        overrides = getattr(self, "board_isolation_overrides", None) or {}
        return overrides.get(key) is not False

    @staticmethod
    def _git_checkout(root) -> bool:
        """Whether `root` is the top of a git checkout: `.git` exists, as a
        folder **or a file** (the project is itself a linked worktree).
        Blocking — one `stat`; executor."""
        text = str(root or "")
        return bool(text) and os.path.exists(os.path.join(text, ".git"))

    #: How long the decoration trusts one `_git_checkout` answer per root.
    GIT_ROOT_MEMO_SECONDS = 30.0

    def _isolation_state(self, root) -> str:
        """`"on"` / `"off"` for a git project, `""` for anything else — what
        a card publishes as `isolation`. Executor; the `.git` test is
        memoised per root for `GIT_ROOT_MEMO_SECONDS`, so a frame of five
        hundred cards is a handful of stats, not five hundred."""
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            return ""
        memo = getattr(self, "_git_root_memo", None)
        if memo is None:
            memo = {}
            self._git_root_memo = memo
        now = time.time()
        hit = memo.get(key)
        if hit is None or now - hit[0] > self.GIT_ROOT_MEMO_SECONDS:
            hit = (now, self._git_checkout(key))
            memo[key] = hit
        if not hit[1]:
            return ""
        return "on" if self._isolation_for(key) else "off"

    # --- Which model each agent and helper runs on ---

    def set_agent_models(self, mapping) -> None:
        """The machine-wide table, `{provider: {slot: model}}`, as stored.

        Cleaned by `agent_models.clean` — an unusable entry is dropped and
        its neighbours kept — and assigned once, `set_board_parallel_overrides`'
        torn-read defence: this runs on the AppKit thread (a press) or before
        the loop exists (the startup feed) while `_agent_model_for` reads on
        the loop. Nothing in a snapshot changes, so there is no republish.
        """
        self.agent_models = agent_models.clean(mapping)

    def set_agent_model_override(self, root: str, mapping) -> None:
        """One project's own table. `mapping` of `None` or an empty table
        removes the entry; otherwise it replaces that root's whole table.
        Same replace-never-mutate rule as `set_board_parallel_override`."""
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            logger.info("ignoring a per-project agent model with no project")
            return
        current = dict(self.agent_model_overrides)
        cleaned = agent_models.clean(mapping, override=True)
        if cleaned:
            current[key] = cleaned
        else:
            current.pop(key, None)
        self.agent_model_overrides = current

    def set_agent_model_overrides(self, mapping) -> None:
        """The whole stored override map at once — the startup feed."""
        built = agent_models.clean_overrides(mapping)
        self.agent_model_overrides = built
        if built:
            logger.info("Per-project agent models loaded for %d project(s)",
                        len(built))

    def _agent_model_for(self, root, provider: str, slot: str) -> str:
        """The model `provider`'s `slot` runs on in *this* project.

        The one resolution seam — override, else the machine-wide table, else
        `agent_models.SHIPPED` — asked by Start, Refine, a consult and
        Prepare, so no launch site can carry a second copy of the rule. The
        answer is re-checked against `agent_models.validate` on the way out
        and `""` (Default: no flag) is returned for anything off-list, so a
        hand-edited `preferences.json` can never put an unknown name on an
        argv.
        """
        provider = str(provider or "")
        slot = str(slot or "")
        if provider not in agent_models.PROVIDERS or slot not in agent_models.SLOTS:
            return ""
        chosen = None
        key = dispatch.normalise_root(str(root or ""))
        if key:
            table = self.agent_model_overrides.get(key) or {}
            row = table.get(provider) or {}
            if slot in row:
                chosen = row[slot]
        if chosen is None:
            row = self.agent_models.get(provider) or {}
            if slot in row:
                chosen = row[slot]
        if chosen is None:
            chosen = agent_models.SHIPPED[provider][slot]
        chosen = str(chosen or "")
        if chosen == agent_models.INHERIT:
            return ""
        return chosen if agent_models.validate(provider, slot, chosen) else ""


    def _record_outcome_binding(self, card, sid, phase, *, late=False):
        try:
            self._board.record_outcome_run(card["id"], card.get("tool"), sid, phase, late=late)
        except Exception:
            self._outcomes_unavailable = True
            logger.warning("outcome run observation unavailable", exc_info=True)

    def _card_observation_facts(self, card, rows, running, active, prompts,
                                observation_states):
        """One per-card projection for outcome waits and lifecycle spans."""
        causes = set()
        visible = not (card.get("session_id") or card.get("refine_session_id"))
        costs = []
        impl_row = None
        for phase, field in (("implementation", "session_id"), ("refinement", "refine_session_id")):
            sid = card.get(field)
            if not sid:
                continue
            self._board.record_outcome_run(card["id"], card.get("tool"), sid, phase, late=True)
            row = rows.get(sid)
            if field == "session_id":
                impl_row = row
            if row:
                visible = True
                if row.get("question") or row.get("pending_questions"):
                    causes.add("question")
                if sid in prompts:
                    causes.add("permission")
                provider = row.get("provider")
                source = row.get("metrics") or {}
                if provider in ("claude", "grok") and source.get("cost_usd") is not None:
                    costs.append(dict(provider=provider, session_id=sid, amount=source["cost_usd"],
                                      currency="USD", source="live_measured", complete=False))
            history = getattr(self, "_history", None)
            if history:
                # The cost fields alone (`session_cost`): the whole
                # `session_record` roll-up per card per pass was the pass's
                # largest read.
                rec = history.session_cost(sid)
                if rec and rec.get("provider") == card.get("tool") and rec.get("cost_source") == "measured":
                    costs = [c for c in costs if c["session_id"] != sid]
                    costs.append(dict(provider=rec["provider"], session_id=sid, amount=rec.get("cost_usd"),
                                      currency="USD", source="history_measured", complete=False))
        working = self._card_session_working(card, running, active)
        if card.get("manual_steps") and not working:
            causes.add("manual_check")
            visible = True
        report = observation_states.get(card["id"])
        awaiting_review = bool(report and report.get("submission_at") is not None
                               and not report["accepted"] and not report["rework_open"])
        if awaiting_review:
            visible = True
        link = str(card.get("link_state") or "")
        sid = str(card.get("session_id") or "")
        executing = (link == "live" and working
                     and "question" not in causes and "permission" not in causes)
        impl_unknown = bool(sid and link in ("live", "dispatching") and impl_row is None)
        seq = {}
        try:
            seq = self._board.lifecycle_facts_seq(card["id"]) or {}
        except Exception:
            seq = {}
        return {
            "causes": causes,
            "visible": visible,
            "costs": costs,
            "working": working,
            "executing": executing,
            "queued": str(card.get("queue_state") or "") == "queued",
            "review": awaiting_review,
            "rework": bool(report and report.get("rework_open")),
            "manual_check": "manual_check" in causes,
            "impl_unknown": impl_unknown,
            "session_id": sid,
            "link_state": link,
            "attempt_id": seq.get("attempt_id"),
            "boundary_seq": seq.get("boundary_seq"),
        }

    def _observe_board_outcomes(self, snapshot, active):
        """Already on the snapshot executor. No transcript discovery or timer.

        Sample every pass (normally `FULL_PUSH_INTERVAL_SECONDS`). Wait
        seconds accrue in memory and reach the ledger on a cause change, a
        day roll, a clean close or every `WAIT_FLUSH_SECONDS`, so crash loss
        is at most that. Nothing is written for a card whose sample
        did not move; the pass is one transaction per ledger, a SAVEPOINT per
        card, so one failing card rolls back alone and raises the flag.
        Monetary sources here establish USD but not child inclusion: partial.
        Lifecycle collection is a sibling: its failure must not mark outcomes
        unavailable or stop reconcile.
        """
        if self._board is None:
            return
        rows = {r.get("session_id"): r for bucket in ("running", "waiting", "sleeping", "finished")
                for r in snapshot.get(bucket, []) if isinstance(r, dict)}
        running = self._board_running_ids(snapshot)
        prompts = self._prompts_by_session()
        facts_by_card = {}
        try:
            observation_states = self._board.outcome_observation_states()
            samples = []
            facts_failed = False
            for card in self._board.cards():
                # Per card: one card whose facts cannot be read must not
                # cost every healthy card its sample.
                try:
                    facts = self._card_observation_facts(
                        card, rows, running, active, prompts, observation_states)
                except Exception:
                    facts_failed = True
                    logger.warning("outcome facts unavailable for card %s",
                                   card.get("id"), exc_info=True)
                    continue
                facts_by_card[card["id"]] = facts
                samples.append((card["id"],
                                facts["causes"] if facts["visible"] else None,
                                facts["costs"]))
            failed = self._board.observe_outcomes(samples)
            self._outcomes_unavailable = bool(failed) or facts_failed
        except Exception:
            self._outcomes_unavailable = True
            logger.warning("outcome measurements unavailable", exc_info=True)
        try:
            hook = getattr(self, "_lifecycle_observe_hook", None)
            if hook is not None:
                hook()
            self._lifecycle_unavailable = bool(self._board.observe_lifecycles(
                [(cid, facts) for cid, facts in facts_by_card.items()]))
        except Exception:
            self._lifecycle_unavailable = True
            logger.warning("lifecycle measurements unavailable", exc_info=True)

    async def decide_card_outcome(self, card_id, revision, key, evidence, *, accept):
        if self._board is None:
            return None, "the board is unavailable"
        async with self._dispatch_lock, self._board_write_lock:
            replay = await self._board_call("outcome_replay", card_id, revision, key, evidence, accept)
            if replay is not None:
                return replay
            card = await self._board_call("get", card_id)
            if card is None:
                return None, "no such card"
            if accept:
                snapshot = self._agents_snapshot_cache or {}
                active = self._claiming_session_ids()
                running = self._board_running_ids(snapshot)
                refining = dict(card, link_state=card.get("refine_state"), session_id=card.get("refine_session_id"))
                if self._card_session_working(card, running, active) or self._card_session_working(refining, running, active):
                    return None, "wait until the assistant has finished before accepting the outcome"
                ids = {card.get("session_id"), card.get("refine_session_id")} - {None, ""}
                rows = [r for bucket in ("running", "waiting", "sleeping") for r in snapshot.get(bucket, [])]
                if ids & set(self._prompts_by_session()) or any(r.get("session_id") in ids and
                        (r.get("question") or r.get("pending_questions")) for r in rows):
                    return None, "answer the outstanding question or permission before accepting"
            try:
                result = await self._board_call("accept_outcome" if accept else "request_outcome_revision",
                                                card_id, revision, key, evidence)
            except Exception:
                logger.warning("outcome decision was not saved", exc_info=True)
                return None, "the outcome could not be saved; your evidence has not been accepted"
        if result and result[0] is not None:
            await self._publish_board()
        return result

    #: What a refused save reports back about the copy the Mac holds. The
    #: fields a person can see and correct, and the revision they are at —
    #: never `messages`, never attachments, never the outcome ring: the 409
    #: body is sealed and a prompt is already up to `MAX_PROMPT_CHARS`.
    STATED_FIELDS = ("revision", "title", "summary", "prompt", "workflow",
                     "column_name", "tool", "model", "priority", "area")

    async def card_stated_fields(self, card_id) -> dict:
        """The Mac's own copy of the fields a save may name. `{}` if gone.

        The "show both" half of the card guard: the phone needs the Mac's
        version *in the refusal*, because the refusal is routinely the first
        thing it hears after being out of signal and a second fetch may not
        be possible. One `_board_call`, so the loop never touches SQLite.
        """
        card = await self._board_call("get", str(card_id or ""))
        if not isinstance(card, dict):
            return {}
        out = {}
        for key in self.STATED_FIELDS:
            value = card.get(key)
            if key == "revision":
                out[key] = int(value or 0)
            else:
                out[key] = str(value or "")
        return out

    async def lifecycle_report(self, **kwargs):
        if self._board is None:
            return {"supported": True, "available": False,
                    "measurements_available": False}
        try:
            report = await self._board_call("lifecycle_report", **kwargs)
            report["measurements_available"] = (
                bool(report.get("measurements_available", True))
                and not getattr(self, "_lifecycle_unavailable", False))
            report["supported"] = True
            return report
        except ValueError:
            raise
        except Exception:
            logger.warning("lifecycle report unavailable", exc_info=True)
            return {"supported": True, "available": False,
                    "measurements_available": False}

    async def card_outcome_report(self, **kwargs):
        if self._board is None:
            return {"supported": True, "available": False}
        try:
            card_id = kwargs.pop("card_id", None)
            report = await self._board_call("outcome_card_report", card_id, **kwargs) if card_id else \
                await self._board_call("outcome_project_report", **kwargs)
            report["measurements_available"] = not getattr(self, "_outcomes_unavailable", False)
            return report
        except ValueError:
            raise
        except Exception:
            logger.warning("outcome report unavailable", exc_info=True)
            return {"supported": True, "available": False, "measurements_available": False}

    #: How many pages of `outcome_project_report` the agent report will walk to
    #: learn which cards were accepted. A page is 100 cards; ten is a whole
    #: project's board several times over, and the bound is here so a pathological
    #: store cannot turn one fetched report into an unbounded walk.
    AGENT_REPORT_MAX_PAGES = 10

    async def agent_efficiency_report(self, range_days, root: str = "") -> dict:
        """What each kind of helper cost, and whether its work was accepted.

        Two databases, joined **in Python** and never by attaching one
        sqlite file to the other's connection:
        `board.db` and `history.db` have separate locks and separate
        lifetimes (history prunes at 90 days, the board does not), and one
        connection spanning both would hold the board's lock for the length
        of a multi-second history scan. So the board half returns its rows and
        releases, the history half runs afterwards on its own hop, and
        `agent_report.join` — pure dict work — folds them.

        Failures answer `{"supported": True, "available": False}` in
        `card_outcome_report`'s shape rather than raising onto the loop: a
        report is an observer of the system, never a gate on it.
        """
        history_store = getattr(self, "_history", None)
        if history_store is None:
            return {"supported": True, "available": False,
                    "reason": "history database is not open"}
        # A shut board is not a shut report: the efficiency half reads
        # `history.db` alone, and `_board_call` answers None on a shut store,
        # so the join simply has no runs and the report says its outcomes half
        # is unavailable.
        try:
            days = None if not range_days else int(range_days)
            end = time.time()
            start = (end - days * 86400) if days else None

            runs = await self._board_call(
                "outcome_run_index", root=root, start=start, end=end) or []
            session_ids = list(dict.fromkeys(
                str(run.get("session_id") or "") for run in runs
                if run.get("session_id")))
            truncated = len(session_ids) > history_store.MAX_JOINED_SESSIONS

            # The roster is the *report's own* scope, not the store's. A
            # report about one project that counted every card on the machine
            # would publish somebody else's outstanding manual checks under
            # this project's heading — so the cards are filtered by the
            # requested root, canonicalised the way `outcome_run_index` and
            # `outcome_project_report` canonicalise theirs.
            wanted_root = str(Path(root).resolve()) if root else ""
            roster: dict = {}
            for card in (await self._board_call("cards") or []):
                card_id = str(card.get("id") or "")
                if not card_id:
                    continue
                card_root = str(card.get("root") or "")
                if wanted_root:
                    if not card_root:
                        continue
                    if str(Path(card_root).resolve()) != wanted_root:
                        continue
                roster[card_id] = {
                    "title": card.get("title") or "",
                    "root": card_root,
                    "manual_steps": card.get("manual_steps") or "",
                    # One character, or a day column counts the card once
                    # per stage. Implementer, else planner, else the first
                    # name in trail order.
                    "who": agent_report.ledger_who(identity.current_crew(
                        board.parse_crew(card.get("crew_trail")))),
                    # No `accepted` and no `rework_count` here on purpose:
                    # absent means nobody asked, which `agent_report.join`
                    # publishes as `null` rather than as a refusal.
                }

            summary, outcomes_reason = {}, ""
            if root and start is not None:
                page, offset = 0, 0
                while page < self.AGENT_REPORT_MAX_PAGES:
                    report = await self._board_call(
                        "outcome_project_report", root=root, start=start,
                        end=end, limit=100, offset=offset) or {}
                    if page == 0:
                        summary = dict(report.get("summary") or {})
                    for row in report.get("cards") or []:
                        stated = roster.setdefault(str(row.get("card_id") or ""), {
                            "title": row.get("title") or "", "root": root,
                            "manual_steps": "", "who": "",
                        })
                        stated["accepted"] = bool(row.get("accepted"))
                        stated["rework_count"] = int(row.get("rework_count") or 0)
                    offset = report.get("next_offset") or 0
                    page += 1
                    if not offset:
                        break
                if not summary:
                    # Asked and answered with nothing, which is not the same
                    # fact as never asked — and both clients gate their
                    # acceptance line on `outcomes_available`, so without a
                    # sentence here the space would simply be blank.
                    outcomes_reason = ("No submitted or accepted work is "
                                       "recorded for this project in this "
                                       "period.")
            elif not root:
                # `outcome_project_report` is scoped to one canonical root by
                # construction (its index is the root index), so a report over
                # every project carries the efficiency half alone and says so
                # rather than inventing a store-wide acceptance figure.
                outcomes_reason = ("Acceptance is reported per project; choose "
                                   "one to see it.")
            else:
                outcomes_reason = ("Acceptance is reported over a bounded "
                                   "period; choose one other than all time.")

            loop = asyncio.get_running_loop()

            # What the helper table is *about*, stated rather than inferred.
            #
            # `session_ids` comes from `outcome_run_index`, which reads
            # `outcome_runs` — so the narrowed table is **the sessions this
            # project's cards were worked by**, not every session that ran in
            # that folder. Ad-hoc work and a refinement nobody bound to a card
            # are absent from it, and both clients caption it in those words
            # rather than as "this project's own sessions", which would claim
            # a completeness `outcome_runs` cannot give.
            #
            # Narrowed **including when there are none**: an empty filter
            # passed on as `None` would fall through to the machine-wide scan
            # and list every helper on the Mac under one project's heading.
            # With no project chosen there is nothing to narrow by, so the
            # table is the machine's and says so.
            scope = "project" if root else "machine"
            filter_ids = session_ids if root else None

            def fetch():
                # One hop for both queries: sqlite3 blocks, and a hop per
                # query would put the loop between two halves of one answer.
                # `days` rides into both: a card's spend must be the spend
                # inside the period this report states, not the whole life of
                # a session that happened to be bound inside it.
                return (history_store.by_agent(days, session_ids=filter_ids),
                        history_store.session_efficiency(session_ids, days=days))

            agent_rows, efficiency = await loop.run_in_executor(None, fetch)
            joined = agent_report.join(runs, agent_rows, efficiency, summary, roster)
            # A second hop, after the board lock is already released: the
            # named cards' sessions are excluded so their dollars are not
            # counted again as "other". Empty means every turn is other.
            named = [
                str(session.get("session_id") or "")
                for card in joined.get("cards") or []
                for session in card.get("sessions") or []
                if session.get("session_id")
            ]

            def fetch_other():
                return history_store.other_daily(days, exclude=named)

            other_days = await loop.run_in_executor(None, fetch_other)
            return {
                "supported": True, "available": True, "root": root,
                "range_days": days, "from": start, "to": end,
                "generated_at": end,
                "sessions_truncated": truncated,
                "agent_scope": scope,
                "outcomes_available": bool(summary),
                "outcomes_reason": outcomes_reason,
                "measurements_available": not getattr(
                    self, "_outcomes_unavailable", False),
                # True until a Codex journal pass has read every journal:
                # the ledger then says Codex is still being read, and its
                # token cost is a floor rather than the period's whole.
                "codex_history_partial": bool(getattr(
                    self, "_codex_history_partial", True)),
                "other_days": other_days,
                **joined,
            }
        except Exception:
            logger.warning("agent efficiency report unavailable", exc_info=True)
            return {"supported": True, "available": False,
                    "reason": "the agent report could not be computed"}
