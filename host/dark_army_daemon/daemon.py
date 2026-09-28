"""Dark Army daemon — bridges Claude Code hooks to the menu-bar strip and
the panel."""

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, NamedTuple, Optional, Protocol, runtime_checkable

import psutil
from filelock import FileLock, Timeout as FileLockTimeout

from .protocol import (
    ASK_USER_QUESTION_TOOL,
    flatten_statusline,
    is_ask_user_question,
)
from .session_stats import clamp_questions, clip, finished_quietly
from . import session_stats as ss
from . import work_report
from .ai_title import first_user_prompt, session_display_name
from .socket_server import SocketServer, HOOK_IPC_PORT
from . import access_log
from . import agents_poll
from . import image_preview
from . import alerts as alerting
from . import board
from . import buzz_ledger
from . import board_workflow
from . import board_queue
from . import codex_rollouts
from . import codex_terminal
from . import codex_titles
from . import codex_input
from . import channel_server
from . import conversation
from . import devices
from . import dispatch
from . import enrollment
from . import event_log
from . import grok_events
from . import prompt_dialogs
from . import grok_leader
from . import grok_roster
from . import history
from . import jobs_store
from . import lan_hosts
from . import link_timing
from . import bearings
from . import fleet_figures
from . import live_activity
from . import mission
from . import origin
from . import power_source
from . import paths as paths_mod
from . import relay
from . import relay_client
from . import relay_ws
from . import run_health
from . import samples
from . import session_registry
from . import areas
from . import identity
from .identity import IdentityStore
from . import pid_resolver
from . import codex_history
from . import grok_scan
from . import terminal_title
from . import transcript_scan
from . import vscode_reveal
from . import ptyhost
from . import session_io
from . import background_watch
from . import subagent_watch
from . import workspace
from . import subprocess_env
from .paths import PREFS_PATH
from .api_server import ApiServer, LAN_API_PORT
from . import session_store
from . import session_title
from . import inbox_ack
from . import card_prepare
from . import card_priority
from . import attachments
from .session_store import save_sessions, load_sessions, load_pending_questions
from .paths import PID_PATH, LOCK_PATH, STATE_DIR, ensure_state_dir
from .daemon_board import BoardVerbsMixin

@dataclass(frozen=True)
class RefinementCloseReceipt:
    """Private, single-use authority for one attached plan and one Codex turn."""

    card_id: str
    root: codex_rollouts.CodexTitleRoot
    project_root: str
    plan_path: str
    journal: tuple[str, int, int]
    turn_id: str
    card_updated_at: float
    expires: float
    state: str = "ready"


REFINEMENT_CLOSE_SECONDS = 600.0
REFINEMENT_CLOSE_TIMEOUT = 8.0
# A shared-server Codex close reads Codex's server twice (the up-front check
# and the extension's before-write check), each bounded at four seconds.
SHARED_CODEX_CLOSE_TIMEOUT = 2 * 4.0 + 6.0


logger = logging.getLogger("dark-army")

TOOL_ANIMATION_MAP = {
    "Edit": "typing",
    "Write": "typing",
    "write": "typing",
    "search_replace": "typing",
    "NotebookEdit": "typing",
    "Read": "debugger",
    "read_file": "debugger",
    "Grep": "debugger",
    "grep": "debugger",
    "Glob": "debugger",
    "list_dir": "debugger",
    "Bash": "building",
    "run_terminal_command": "building",
    "Agent": "conducting",
    "Task": "conducting",
    "spawn_subagent": "conducting",
    "WebSearch": "wizard",
    "web_search": "wizard",
    "WebFetch": "wizard",
    "web_fetch": "wizard",
    "LSP": "beacon",
}


# Interchangeable looks for one animation, so a screen full of Bobs doing the same
# thing isn't a screen full of identical Bobs. Purely cosmetic: every entry in a pool
# means exactly what the key means, so which one a session draws carries no signal.
ANIMATION_VARIANTS = {
    "typing": ("typing", "tongue_zap"),
}


def _still_our_process(pid: int, provider: Optional[str] = None) -> bool:
    """Is `pid` still a live coding-agent process — not just a live *something*?

    The liveness evictor's question is "did the process we recorded go away", and a
    recycled PID answers that wrongly under `pid_exists`. Uses the same rule as the
    signal path (`pid_resolver.looks_like_session`, cmdline not name — a node-wrapped
    Claude install reports "node").

    Errs toward *alive*: when the process exists but cannot be inspected, keeping a
    row that should have gone costs a stale entry, while evicting one that is still
    working takes a running session's face off the screen.
    """
    try:
        proc = psutil.Process(pid)
        return pid_resolver.looks_like_session(
            proc.name(), " ".join(proc.cmdline() or []), provider,
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return True


def _grok_session_pid_ok(pid: Optional[int]) -> bool:
    """True when `pid` is a live Grok *session* — not the shared leader.

    After `use_leader`, hooks run under `grok agent leader` and stamp that
    number on every session. The leader has no tty, so TitleWriter skips
    the tab, and the same pid on every row makes `/clear` dedup evict
    siblings. `_still_our_process` already excludes the leader once
    `looks_like_grok` does.
    """
    return pid is not None and _still_our_process(pid, "grok")


def _is_live_grok_leader(pid: Optional[int]) -> bool:
    """True when `pid` is the shared `grok agent leader`, still running.

    Distinct from `_grok_session_pid_ok` (False for both the leader and a
    recycled Dock). Liveness must not treat the live leader as "PID gone",
    but a number that now names somebody else still has to evict.
    """
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        return pid_resolver.is_grok_leader(" ".join(proc.cmdline() or []))
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return False


def _process_cwd(pid: int) -> Optional[str]:
    """The process's cwd, or None when we cannot tell.

    None is "do not evict": a cwd we cannot read is the same class of
    uncertainty `_still_our_process` already treats as alive.
    """
    try:
        return psutil.Process(pid).cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def _pid_descends(pid: int, ancestor: int) -> bool:
    """Whether `pid` is `ancestor` or one of its descendants.

    The proof behind the terminal receipt: the extension spawns the agent
    binary *directly* as the terminal process, so a session that came out of
    Dark Army's terminal is that shell pid or a child of it. Every failure — a pid
    that is gone, unreadable, or not an int — answers False, because the
    caller skips an unproven row and retries on the next snapshot rather than
    binding on uncertainty. Pid reuse inside the 120s bind window could in
    principle satisfy this; bounded by the window and the provider/project/
    baseline checks around it, and accepted with the same candour as the
    identity guards elsewhere.
    """
    try:
        pid = int(pid)
        ancestor = int(ancestor)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or ancestor <= 0:
        return False
    if pid == ancestor:
        return True
    try:
        return any(p.pid == ancestor for p in psutil.Process(pid).parents())
    except (psutil.Error, OSError):
        return False


def _same_cwd(left: str, right: str) -> Optional[bool]:
    """True if both resolve to the same path. None if we cannot tell.

    ``normpath`` misses the /tmp → /private/tmp class of inequality that
    dropped live Grok rows. ``realpath`` both sides; an unresolvable
    path is "keep the row", same as a cwd we cannot read.
    """
    try:
        a = os.path.realpath(left)
        b = os.path.realpath(right)
    except OSError:
        return None
    return a == b


#: A recipient may be typed with the disambiguator `ListAgents` prints after a
#: name — `"finance-demo-27 [cb55e0]"`. The registry holds the bare name, so an
#: exact join reports a live, addressable peer as gone.
_ADDRESS_REF = re.compile(r"\s*\[[0-9a-f]{4,16}\]\s*$", re.IGNORECASE)


def _bare_address(address: str) -> str:
    """The recipient without the `[ref]` a sender may have typed after it.

    Found the way the subagent case was: by sending one. The first edge drawn
    after the inbound view existed was addressed to
    `"Reply with exactly the word ok and stop. [5ccbdc]"` — a session that was
    live, addressable and *in the same snapshot*, reported as `resolved: false`
    because the registry knows it by name alone.

    The ref is deliberately **not** matched against session ids: that one was
    `5ccbdc` for a session `7d8bf643…`, so it names something other than the id
    Dark Army keys on, and a fallback that happened to hit would be a coincidence
    rather than a rule.
    """
    return _ADDRESS_REF.sub("", address or "")


def _variant_index(session_id: str, pool_size: int) -> int:
    """Pick a pool slot for a session: stable for its whole life, and the same
    after a daemon restart (session ids outlive us in sessions.json).

    Deliberately not `hash()` — str hashing is salted per interpreter, so Dark Army
    would change costume every time the daemon came back up.
    """
    digest = hashlib.blake2b(session_id.encode(), digest_size=2).digest()
    return int.from_bytes(digest, "big") % pool_size


def _tool_to_anim(tool_name: str, session_id: str = "") -> str:
    if tool_name and tool_name.startswith("mcp__"):
        base = "beacon"
    elif (
        tool_name
        and "__" in tool_name
        and tool_name not in TOOL_ANIMATION_MAP
    ):
        # Grok MCP tools arrive as server__tool (gitnexus__query), not mcp__.
        base = "beacon"
    else:
        base = TOOL_ANIMATION_MAP.get(tool_name, "typing")
    pool = ANIMATION_VARIANTS.get(base)
    if not pool or not session_id:
        return base
    return pool[_variant_index(session_id, len(pool))]


# A session has to have been busy at least this long for its Stop to be worth
# celebrating. Without it every one-line answer sets off the confetti and the
# burst stops meaning "that took a while and it's done".


# Sound played (fire-and-forget via macOS `afplay`) when a new notification card
# appears. Set to "" to disable. This replaces the old Claude Code `Notification`
# hook that both showed a macOS banner AND played Pop.aiff — the banner is gone and
# the sound now lives here, tied to the card actually surfacing on the companion.
NOTIFICATION_SOUND_PATH = "/System/Library/Sounds/Pop.aiff"

# The chime is debounced by this window before it plays (see
# BobDaemon._schedule_notification_sound). Phantom cards — dismissed within
# ~70ms by an immediate UserPromptSubmit — are an order of magnitude under this,
# so they cancel themselves; a real "Waiting for input" card persists for
# seconds and survives it cleanly.
NOTIFICATION_SOUND_DEBOUNCE_SECONDS = 0.5


def _play_notification_sound() -> None:
    """Play the notification sound without blocking the event loop.

    Best-effort: swallows every error (missing file, no `afplay`, non-macOS) so a
    sound-system hiccup can never disrupt notification delivery."""
    path = NOTIFICATION_SOUND_PATH
    if not path or not os.path.exists(path):
        return
    try:
        import subprocess
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        logger.debug("Could not play notification sound", exc_info=True)


PID_DEDUP_FRESHNESS_SECONDS = 60.0

# A session with no stored PID was registered by a pre-PID hook and has emitted no
# event since (any current hook event backfills the PID). If it also stays silent
# past this grace window it is a dead ghost and gets evicted by the liveness check.
PIDLESS_GRACE_SECONDS = 90.0
#: The end verdict for a session whose Dark Army-hosted terminal is no longer held
#: by the broker — the process inside it died with the terminal. Drawn on the
#: tombstone; the panel and the phone show `end_reason` as it is.
TERMINAL_CLOSED_REASON = "terminal closed"
# The one refusal `wrap_up_session`, `close_session_terminal` and
# `wrap_up_or_close_session` share. A constant rather than a fifth literal,
# because the third compares the second's answer against it.
PROMPT_BLOCKED_REFUSAL = ("That session is waiting on a permission prompt — "
                         "answer it first.")
#: The one fact four refusals share: no VS Code window with Dark Army's extension
#: owns this session's terminal. Written once, so the two verbs it ends —
#: typing and closing — can never drift into two different descriptions of
#: the same missing window.
_NO_WINDOW_PREFIX = ("No VS Code window owns this session's terminal, "
                     "so Dark Army cannot ")
#: What `wrap_up_session`, `low_priority_session`, `answer_questions` and
#: `message_card` all end on when `send_text` did not land — unless the
#: editor said why (`_typed_nothing_refusal`).
NO_TYPING_WINDOW_REFUSAL = _NO_WINDOW_PREFIX + "type into it."


def _typed_nothing_refusal(result) -> str:
    """The editor's own words for a keystroke it refused before typing
    anything, or "". Only a reply that says `sent: false` explicitly and
    carries an `error` — the bridge's Workspace Trust refusal, which the
    router (`session_io.send_text`) hands up from the owning window — so an old
    window's `unknown op`, a timeout or a silent miss keep the caller's own
    refusal."""
    if not isinstance(result, dict) or result.get("sent") is not False:
        return ""
    error = result.get("error")
    return error.strip() if isinstance(error, str) else ""
#: `close_session_terminal`'s own half of the same fact.
NO_CLOSING_WINDOW_REFUSAL = _NO_WINDOW_PREFIX + "close it."
#: `terminal_input`'s refusals — typing a line into a terminal Dark Army itself
#: owns (`ptyhost`). Each is its own sentence rather than a reuse of the
#: window refusals above: a Dark Army-run terminal has no window to be missing.
TERMINAL_NOT_OWNED_REFUSAL = ("Dark Army does not own this session's terminal, "
                              "so it cannot type into it from here.")
TERMINAL_EXITED_REFUSAL = "That terminal's process has ended; nothing is reading it."
TERMINAL_PROMPT_REFUSAL = ("That session is waiting on a permission prompt — "
                           "answer it at the Mac first; nothing is typed.")
TERMINAL_EMPTY_REFUSAL = "Type something first."
TERMINAL_ONE_LINE_REFUSAL = "One line at a time: the terminal reads a newline as Enter."
TERMINAL_TOO_LONG_REFUSAL = "That is too long for one line of terminal input."
#: The phone may not type a control character: two Ctrl-Cs quit the CLI,
#: Escape cancels a turn and an arrow-key sequence recalls history —
#: `message_card`'s own test (`ord(c) < 32 or ord(c) == 127`), for its
#: reason, and the third thing a terminal can do that a channel cannot.
TERMINAL_CONTROL_REFUSAL = ("Control characters are not typed from the phone — "
                            "Ctrl-C, Escape and the arrow keys belong in front "
                            "of the machine.")
#: The pty took only part of the line within `TERMINAL_DRAIN_SECONDS`. Its
#: own sentence and never `TERMINAL_EXITED_REFUSAL`: Darwin's pty input
#: queue is ~1 KB, so a long line to a CLI mid-redraw is a wait, not a death.
TERMINAL_BUSY_REFUSAL = ("The terminal is not taking input right now — part of "
                         "that line may have landed; check the screen before "
                         "typing it again.")
#: How long `terminal_input` waits for a queued remainder to land.
TERMINAL_DRAIN_SECONDS = 3.0
#: Raw desk input per frame or per request. A paste, not a line: the
#: extension path never had a cap this low and the pty queues what it
#: cannot take at once (`PtyHost.write`).
TERMINAL_MAX_RAW_BYTES = 256 * 1024
#: The most a `terminal_input` press may carry.
TERMINAL_MAX_INPUT_CHARS = 2000
#: How many bytes of changed rows one terminal frame may carry before `more`
#: is stated — `CARD_SYNC_MAX_BYTES`' figure and its reason.
TERMINAL_MAX_BYTES = 200_000
#: Raw pty bytes one `since_bytes` poll may carry — the away phone's feed,
#: one hop per 8 s poll and never a `more` loop. Base64 makes it 256 000 on
#: the wire, inside one sealed `terminal` reply under
#: `relay.RELAY_FRAME_MAX_BYTES` (deflate is a no-op on TUI output, so the
#: figure is chosen for the raw case). `data_more` says the rest is waiting.
TERMINAL_POLL_RAW_BYTES = 192_000
#: How long the remainder of a bounded away paint is kept for the phone's
#: next poll (`_terminal_paint_tail`). A paint over `TERMINAL_POLL_RAW_BYTES`
#: is served in slices across several check-ins, each quoting the same
#: anchor cursor back; a phone that went quiet leaves its tail behind, and
#: this is when the tail is let go.
TERMINAL_PAINT_TAIL_SECONDS = 120.0
#: `message_card`'s own refusals. Eight sentences rather than one, because a
#: person who is turned down needs to know *which* of the eight it was: four
#: are about the text they typed (empty, a leading `/`, more than one line,
#: too long) and four about the card they typed it on (nobody working it, a
#: Codex assistant, no process on record, a question already on its input
#: line).
CARD_MESSAGE_EMPTY_REFUSAL = "A message needs some text."
CARD_MESSAGE_SLASH_REFUSAL = (
    "A message starting with `/` would run as a command in that terminal "
    "rather than be read. Type it in the terminal if you mean that.")
CARD_MESSAGE_ONE_LINE_REFUSAL = (
    "A message has to be one line — a line break would send it before you "
    "had finished.")
CARD_MESSAGE_NO_SESSION_REFUSAL = (
    "Nobody is working this card, so there is no terminal to type into.")
CARD_MESSAGE_CODEX_REFUSAL = (
    "Dark Army cannot type at a Codex session; say it in the original Codex "
    "session.")
CARD_MESSAGE_NO_PID_REFUSAL = (
    "Dark Army has no process on record for this card's session.")
#: The last of the four about the card, and a sibling of
#: `PROMPT_BLOCKED_REFUSAL` rather than a reuse
#: of it: an `AskUserQuestion` dialog is deliberately *not* a permission
#: prompt (the hook broker refuses to hold one, because the question flow owns
#: that dialog), so it is absent from `_prompts_by_session()` and needs its own
#: gate and its own sentence. The hazard is the same newline: the dialog owns
#: the input line, so a message typed here would discard what the person was
#: reading and commit an answer nobody chose.
CARD_MESSAGE_QUESTION_REFUSAL = (
    "That session is waiting on a question — the dialog owns its input line, "
    "so a message would answer it instead of being read. Answer the question "
    "first.")
#: The last of the four about the text,
#: `BobDaemon.CARD_MESSAGE_TOO_LONG_REFUSAL`, lives on the class
#: rather than here: it is composed once from `BobDaemon.MAX_REPLY_CHARS`, and
#: a second copy of that number is exactly the drift this block exists to stop.
#: The note a card carries when a person closed its session's terminal.
#: `declare_done` refuses an empty note and clamps at 400 characters; this is
#: the sentence a human reads on the card, so it says who acted and why — the
#: store's `closed_by` column can only hold the session's own id.
CLOSE_TERMINAL_DONE_NOTE = (
    "Somebody pressed Close terminal on this session in Dark Army, "
    "so its card was finished.")
# Close already decided this pid is not the Codex process we recorded
# (`matching_process_identity`). `wrap_up_or_close_session` must not then
# type `/clear` at `_roster_pid` — a recycled pid is the wrong tab.
CODEX_IDENTITY_REFUSAL = ("Codex process identity changed; "
                          "the terminal was not closed.")
# `low_priority_session`'s three refusals of its own (the fourth is
# PROMPT_BLOCKED_REFUSAL above). `/low-priority` is a Claude Code command
# that answers one failure — a rate-limit `StopFailure` — and it is a toggle,
# which is what the third sentence is about.
LOW_PRIORITY_NOT_CLAUDE_REFUSAL = ("Low priority is a Claude Code command; "
                                   "this session is not Claude Code.")
LOW_PRIORITY_NOT_LIMITED_REFUSAL = ("That session is not rate-limited right "
                                    "now, so there is nothing to switch.")
LOW_PRIORITY_ALREADY_REFUSAL = ("Low priority was already switched on for "
                                "this session a few minutes ago — running it "
                                "again would switch it off. Use the terminal "
                                "if you mean that.")

# How many distinct subagent names one session's append-only `subagents_seen`
# will hold. Unlike `subagents`, that list never shrinks, so a long-running
# orchestrator that spawns a fresh agent every few minutes would otherwise grow
# it without bound for as long as the session lives. Well past any real pipeline
# — `/ship` runs five — and the board applies its own, smaller bound again at
# the store.
MAX_SEEN_SUBAGENTS = 40
# How long a live-subagent entry may park its parent with no
# `subagent_start`/`subagent_stop` traffic at all. Hook delivery is
# best-effort, so a dropped SubagentStop used to leave the parent's `subagents`
# set populated for the life of the session — and `_parked_reason` then
# suppressed every one of its "Waiting for input" cards for ever. Longer than
# any plausible single subagent turn; a genuinely live child refreshes the
# stamp on its own stop. Bounds the *parking* only — the set itself is still
# cleared by `subagent_stop`, `session_start` and a fresh `UserPromptSubmit`.
SUBAGENT_PARK_MAX_SECONDS = 30 * 60
# The shortest all-hex name still read as an opaque id rather than a stage.
# Above any real agent type, which needs letters outside `a-f` to get this
# long; below the 17-character ids the harness actually mints. See
# `BobDaemon._stage_name_ok`.
OPAQUE_ID_MIN_CHARS = 12

# Grok writes ~/.grok/active_sessions.json a moment after SessionStart, and a
# brand-new session is routinely missing from the first read. Past this window
# a hook-tracked Grok session that the roster does not list is a leftover —
# Grok does not always fire SessionEnd when the tab closes, and the teardown
# Stop used to be dropped, so the roster is the only timely signal.
GROK_ROSTER_GRACE_SECONDS = 20.0

# A Grok session whose tab has gone is not over. The turn runs under the
# shared `grok agent leader`, which outlives the terminal — VS Code's main
# process ran out of memory on 21 Sep 2026 and every tab died, while the
# session on card 982367ab kept implementing, spawned its verifier and
# flagged its hand-check with no terminal at all. Grok drops the id from
# `active_sessions.json` with the tab, so the roster says "gone"; the
# session directory says otherwise, and for as long as it (or a live
# child's) was written inside this window, a mid-turn row stays. Only
# mid-turn: a row that reached Stop and then lost its tab is finished, and
# ends at the roster grace as before.
GROK_HEADLESS_GRACE_SECONDS = 300.0

# How long the phone's live card waits before the same body is sent again
# after the mailbox could not be reached (no answer, a 5xx). The live card
# shares the buzz's `_push_bucket` (10 a minute), and a pass every few seconds
# retrying an unreachable mailbox would drain it — the buzz starved behind a
# card. Two retries a minute leaves the buzz eight. A *refused* body is never
# retried at all; see `_push_live_activity`.
LIVE_ACTIVITY_RETRY_SECONDS = 30.0

# How long a shape-2 card holds a change that is only cost or tokens.
# Counts, the face and an end send at once; a figures tick does not, or
# five working agents would spend the bucket the buzz shares.
# `LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS` is that hold.
LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS = 60.0


def _quiet_stamp(value) -> float:
    """A row's `quiet_since`: the source's own stamp as whole seconds, or
    0.0 (undated) where the source has none — never the current clock,
    which would move every frame on a stampless row."""
    try:
        stamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if stamp != stamp or stamp in (float("inf"), float("-inf")) or stamp <= 0:
        return 0.0
    return float(int(round(stamp)))


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The event loop running on *this* thread, or None — in an executor
    thread and outside any loop alike. Safe from either side of the seam,
    which is why the decide family asks here rather than calling
    `asyncio.get_running_loop()` itself (`test_thread_placement.py`)."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None

# Longstop for a relayed permission prompt nothing ever resolved. Long, because
# a session blocked while its human is away is the case this feature is for —
# see `_reap_permissions`, where the load-bearing signals are the other two.
PERMISSION_STALE_SECONDS = 30 * 60.0

# A hook-brokered prompt has no channel, so `_reap_permissions`' first and
# strongest signal — "the channel is gone" — cannot speak for it. Its
# substitute is the broker's own poll: the installed script comes back every
# two seconds for as long as it is holding, so a lapse this long means it
# exited, and a hook script exits for exactly the reasons a prompt ends —
# answered at the desk, given up on, or the session died. Without this a hook
# row would pin its session under "Needs you" for the life of the daemon.
HOOK_PROMPT_POLL_LAPSE_SECONDS = 15.0

# How long the daemon asks the broker to hold, and therefore how long a hook
# row may sit before it is reaped on age alone. Kept at the script's own
# BROKER_WAIT_SECONDS twin so the two never disagree about who gave up first.
#
# **The hold and the longstop are now one figure**, written as the name so
# they can never drift apart again: `hold_until` is `asked_at +
# PERMISSION_STALE_SECONDS`, exactly where the age-alone rung below it sits.
# `hold_until` wins by evaluation order — its `if why:` block `continue`s
# before the stale test is reached — so an expired hook row's log line is
# always "the broker's hold ran out", and PERMISSION_STALE_SECONDS goes on
# governing channel rows alone. The load-bearing signal is unchanged and is
# not this: HOOK_PROMPT_POLL_LAPSE_SECONDS drops a row on evidence rather
# than on a clock, and does not move with the hold.
#
# Minutes rather than seconds, and only because it was measured. The CLI's
# `awaitAutomatedChecksBeforeDialog` mode runs the hooks *before* drawing the
# dialog, and on that path a hold is a freeze with nothing on screen to
# answer; that is why this used to be set to what was survivable if the
# ordinary path behaved the same way. It does not, on Claude Code 2.1.263 —
# `docs/2026-09-07-permission-hold-verification.md` holds the run: the dialog
# stayed drawn and took a cursor key mid-hold, and whichever side answered
# first won. **A CLI bump re-runs `tools/permission_hold_livefire.py`** and
# this figure follows its VERDICT line. The record does not speak for a
# subagent's ask, which `_handle_permission_ask` refuses outright.
HOOK_PROMPT_HOLD_SECONDS = PERMISSION_STALE_SECONDS
CODEX_HOLD_ENABLED = False  # Flipped only after a live CONCURRENT verdict.
PROMPT_NOT_HOSTED_NOTE = (
    "Answer this in its terminal — Dark Army did not open that terminal.")
GROK_ANSWER_CONFIRM_SECONDS = 10.0

# The states that say a hook-brokered session's *turn* has moved past the ask,
# and therefore that the dialog the row stands for is off the screen. Read only
# by `_reap_permissions`' hook leg, and only alongside a `last_event` later than
# the ask.
#
# Why this is a named set and not "any newer event". A session sitting blocked
# on a permission dialog keeps emitting: Claude Code sends a `Notification`
# (`notification_type=permission_prompt`) a few seconds behind the
# `PermissionRequest`, and another every ~60s of quiet after that. Both land in
# `_update_session_state`'s `add` branch as `hook == "Notification"`, which sets
# the state to **`confused`** and moves `last_event`. A bare `last_event >
# asked_at` test would therefore drop nearly every hook row seconds after it
# opened — the exact opposite failure, and the reason the channel leg's own
# resume rung is worded the way it is.
#
# `working` / `thinking` are that rung's pair. `idle` (a `Stop`) and `error` (a
# `StopFailure`) are added here because the desk-answer shape ends in one of
# them: the person answers the dialog at the terminal, the tool runs, the turn
# finishes, and nothing ever returns to `working`. Without them such a row
# survived to `hold_until` — half a minute once, half an hour now — shadowing
# the session's next genuine ask in `_prompts_by_session` and holding five verbs
# (reply, typing, wrap up, low priority, `can_close`) refused behind it.
HOOK_PROMPT_TURN_OVER_STATES = ("working", "thinking", "idle", "error")

# The broker's own fields, clamped again here: the script on disk is a copy
# and may be older than this daemon, so the bounds it applied are not the
# bounds this process is willing to publish.
MAX_PROMPT_DESCRIPTION_CHARS = 300
MAX_PROMPT_INPUT_CHARS = 400

# Ctrl-U: kill the input line before typing onto it. Sent as a prefix rather
# than as its own `sendText`, so the clear and the command cannot be separated
# by something the user types between two round-trips.
INPUT_LINE_CLEAR = "\x15"

# What "wrap up" types. Claude Code spells it `/clear` and expands it on the
# input line — which is the only reason this can work at all: the channel
# lands in the model's *reading*, and no model has a tool that ends its own
# conversation. Grok is refused before this is typed (`GROK_WRAP_UP_REFUSAL`):
# `/clear` starts a new Grok id and never SessionEnds the old one.
WRAP_UP_COMMAND = "/clear"
GROK_WRAP_UP_REFUSAL = (
    "Grok stays open so you can read what was done. "
    "Close is the control that disposes it.")

#: First characters a typed reply is refused on (`_reply_by_typing`). Each is
#: expanded by the client on the input line before any request exists, so
#: typed they are not words: `/` is a slash command, `!` is bash mode, `#` is
#: the memory shortcut. The refusal names the character.
TYPED_REPLY_REFUSED_PREFIXES = "/!#"

# What the Low priority button types. Claude Code expands it on the input
# line, like `/clear`, so it goes through `send_text` and never the channel.
# It is a **toggle**: typed twice it switches low priority back off, which
# is why `_low_priority_sent` remembers a success for this long and both the
# verb and the `can_low_priority` flag read that memory.
LOW_PRIORITY_COMMAND = "/low-priority"
LOW_PRIORITY_COOLDOWN_SECONDS = 600.0

# The plan gate's refusal, and it is a **contract, not just a sentence**: the
# panel recognises this refusal (`ActionResult.isPlanGateRefusal` matches its
# opening words) and answers it by raising the same "Start unplanned?"
# confirmation the unplanned routes already show — which is what makes the
# gate a confirmation rather than a wall even when a card's `plan_path` names
# a file that has since moved (a branch switch does that to `plans/`
# routinely). Every state this refusal describes must have a reachable
# on-screen route that produces the confirmation; a test in
# `test_board_refine.py` pins the wording against the panel's matcher, so
# reword both sides together or the degraded mode is the refusal drawn as
# plain words with no way through.
PLAN_GATE_REFUSAL = ("this card has no plan yet — refine it first, or confirm "
                     "starting without one")

# The gate's second rung, at v15, and deliberately a **second constant rather
# than a reuse of the one above**. Both surfaces recognise the gate by its
# opening words, so reusing `PLAN_GATE_REFUSAL` here would be free — and would
# put a sentence on screen that is false, under a confirmation whose armed
# label reads "Start unplanned?" for a card that has a plan somebody simply
# has not re-read. Confirming that would mean something different from what it
# says. Neither constant may ever become a prefix of the other; a test in
# `test_board_refine.py` pins both against both surfaces' matchers and against
# each other.
PLAN_CHANGED_REFUSAL = ("this card's plan has changed since it was approved — "
                        "read it again, or confirm starting anyway")

# A card's Expected specialists box may only name a helper the card's own
# project declares. Refused at the write, which is the one moment somebody can
# still fix the spelling — later it is a stranger's face on the card with no
# job under it, and no route back to the person who typed it. It names the
# offending helper, because "one of these is wrong" is not actionable. Only a
# *changed* workflow is judged (`daemon_board`), so no existing card becomes
# unsavable, and an unreadable roster refuses nothing.
OFF_ROSTER_STAGE_REFUSAL = ("this card names a helper this project does not "
                            "have: %s — use one of the project's own helpers, "
                            "or leave the box empty")

# What a message from a project Dark Army was never told to watch is told, in the one
# place a refusal can be *heard*: the five board verbs ride a `tools/call`, and
# an unanswered one is a session waiting for ever. Everything else on the hook
# socket is fire-and-forget and simply goes nowhere.
ENROLLMENT_REFUSAL = ("this project is not enrolled with Dark Army — enrol it from "
                      "Dark Army's panel (⋯ → Projects) to use the board from here")

# The two knowledge verbs' own refusals. Both name what did *not* happen: a
# model told only "refused" will try again with a project name, which is the
# one thing these verbs must never accept.
KNOWLEDGE_UNPLACED_REFUSAL = ("Dark Army could not tell which project this "
                              "session is in, so it wrote nothing")
KNOWLEDGE_STORE_REFUSAL = "Dark Army's board is not open — nothing was stored"

# The preferences a *phone* may ask the Mac to change, and the whole set of
# them. Named here rather than matched with a prefix, for `LAN_ACTIONS`'
# reason: a prefix test would enrol whatever a future preference happens to be
# called, and the point of this gate is that it is a list somebody chose.
# `board_dispatch` is deliberately absent — turning Dark Army's launcher on or off
# is a capability grant and stays at the desk.
PHONE_PREFERENCES = ("board_autostart", "board_parallel_root")

# What a phone is told when nobody can act on the request. The daemon does not
# store preferences — `preferences.json` is the menu-bar app's file, and one
# writer is the whole design — so with the app absent (a headless daemon, a
# test) there is nothing to hand the press to. Refused in words rather than
# accepted and dropped: a switch that reports success and changes nothing is
# worse than one that says it cannot.
PREFERENCE_UNREACHABLE_REFUSAL = (
    "Dark Army's menu-bar app is not listening, so this cannot be changed from the "
    "phone.")

# How many folders Dark Army will remember having turned away, so the panel can offer
# to enrol one. Memory only: a quarantine that survived a restart would be a
# durable object an unauthenticated socket can create.
MAX_UNENROLLED = 16


def checklist_facts(roots, open_windows, rows, root_enrolled=None,
                    own_root="") -> tuple:
    """The first-run checklist's evidence, per enrolled root, as plain facts.

    Pure: `roots` is the ledger's canonical roots, `open_windows` is
    `workspace.windows()`'s answer (each with its lock pid), `rows` are the
    admitted **main-agent** rows of the current fleet — the caller has already
    filtered helpers, background agents and finished/abandoned rows —
    `root_enrolled` maps a cwd to its enrolled root (`enrollment.root_enrolled`
    in production) and `own_root` is Dark Army's own checkout
    (`enrollment.self_root()`, "" when there is none). One entry per root,
    sorted, each ``{"root", "editor_observed", "session_observed",
    "own_checkout"}``:

    - **editor_observed** is that exact folder being a workspace folder of a
      window whose extension host is still alive. A containing parent, a
      similarly named sibling and a nested enrolment inside it all read
      false — the checklist tells a newcomer to open *this* folder — and a
      dead lock is no evidence of anything.
    - **session_observed** is a current admitted main session whose cwd
      resolves to this exact root, whatever its state: registered, idle,
      parked and unprompted all count, because starting a session does not
      require sending a prompt. An empty or unresolvable cwd counts for
      nobody.
    - **own_checkout** is that root being exactly `own_root` — the checkout
      `enroll_self()` enrolled, not a folder the person chose. The panel's
      checklist never follows it or offers it in the folder picker. An
      empty `own_root` marks nothing.

    No key, digest, session id, pid or session content leaves here.
    """
    resolve = root_enrolled or enrollment.root_enrolled
    open_folders: set = set()
    for window in open_windows or ():
        pid = int(getattr(window, "pid", 0) or 0)
        if not workspace.pid_alive(pid):
            continue
        for folder in getattr(window, "folders", ()) or ():
            try:
                norm = dispatch.normalise_root(str(folder))
            except (OSError, ValueError):
                norm = ""
            if norm:
                open_folders.add(norm)
    session_roots: set = set()
    for row in rows or ():
        cwd = str((row or {}).get("cwd") or "")
        if not cwd:
            continue
        try:
            hit = resolve(cwd)
        except Exception:
            hit = ""
        if hit:
            session_roots.add(hit)
    out = []
    for root in sorted({str(r) for r in (roots or ()) if r}):
        out.append({
            "root": root,
            "editor_observed": root in open_folders,
            "session_observed": root in session_roots,
            "own_checkout": bool(own_root) and root == own_root,
        })
    return tuple(out)

# How recently a Prep card must have been authored for `attach_plan_by_session`
# rung 2 to claim it. Rung 2 exists for one flow — Phase 5 files the card and
# the *very next tool call* attaches the plan to it, two consecutive calls
# seconds apart — so ten minutes is already generous (it tolerates a
# permission prompt sitting unanswered between the two). Anything older is far
# likelier the stale-note case: a session that filed one unrelated card via
# `dark_army_add_card` earlier in its life would otherwise have this plan attached
# to that note, and nothing clears `plan_path` (`reset_card` leaves it,
# `attach_plan` refuses a non-empty one), so a mis-attach is uncorrectable
# short of deleting the card. The window errs closed on purpose: a refused
# legitimate attach is recovered by the skill's own fallback (file a fresh
# card, attach again — the new card is seconds old), while a wrong attach has
# no undo.
ATTACH_AUTHOR_WINDOW_SECONDS = 10 * 60.0

# How many agents may work at once in one project, as the `board_parallel`
# preference is allowed to say. The **floor is 1, not 0**: a 0 would park
# every project's queue for ever, which is `board_autostart` off wearing a
# number's clothes, and two spellings of one switch is not a switch. The
# **ceiling is 4** because the accepted risk — two agents in one tree — grows
# pairwise (one chance-pair at 2, six at 4) while `MAX_QUEUED_PER_PROJECT` (8)
# already says a project's pipeline is meant to fit on a screen; past 4 the
# dial is asking for a different product. It sits deliberately *above*
# `dispatch.MAX_CONCURRENT_DISPATCH` (2), which bounds cards **starting**
# rather than running: slots above that fill one at a time as each card binds,
# which is the designed shape and not a stall.
BOARD_PARALLEL_MIN = 1
BOARD_PARALLEL_MAX = 4


def _queue_reason(count: int, autostart: bool) -> str:
    """What a queued card says about its wait, in one sentence.

    Composed here — server side, published per card as `queue_reason` — rather
    than in the panel, so the refusal a person reads at the moment they press
    Start and the badge the card wears a snapshot later are the same words. It
    is composed at *decoration* time and never remembered from the reconcile,
    which is what lets flipping `board_autostart` change every queued card's
    wording on the next frame instead of at the next unrelated card edit.

    Two axes and nothing else. The head is the **promise**: with the drain off
    nobody is coming, and a card that says otherwise is the app promising an
    action it will not take. The tail is the **count** of agents working in
    that project, which under the slot rule is the whole reason the card is
    waiting — no single holder exists to name, because the project is full
    rather than a particular card being in the way.

    A count of zero is the honest gap: between two reconciles a card can be
    queued with nothing running (a transient launch bound, almost always),
    and naming zero agents would be a sentence about nobody.
    """
    head = ("Queued — Dark Army will start it" if autostart
            else "Queued — press Start")
    n = max(0, int(count or 0))
    if n <= 0:
        return f"{head} shortly" if autostart else head
    if n == 1:
        return f"{head} when the agent working in this project finishes"
    return (f"{head} when one of the {n} agents working in this project "
            "finishes")


#: How many lines of "this one was left alone" one `start_project` reply may
#: carry before it stops being a report and becomes a log. Eight is
#: `MAX_QUEUED_PER_PROJECT`, deliberately: past the point where the queue
#: itself is full, naming another card by title tells the reader nothing the
#: tail line does not.
START_PROJECT_MAX_LINES = 8


def _start_project_report(started: int, queued: int, already: int,
                          skips: list, remaining: int = 0,
                          remaining_reason: str = "",
                          max_cards: int = 0) -> str:
    """What one press of START PROJECT answers, in words. Pure.

    `_queue_reason`'s rule one gesture up: the sentences a person reads about
    a batch are composed **here**, server side, and both clients draw the
    string verbatim. A panel and a phone each phrasing "three were left alone"
    for themselves is two wordings of one press that will drift, and the
    reply is the only place a skipped card is reported at all — the batch
    deliberately writes no `dispatch_error`, because a Backlog card with no
    plan behind it is the plan gate working as designed rather than a card
    that failed.

    `started` and `queued` are one term on purpose: from the operator's side
    the press did the same thing to both cards — it put them in the pipeline —
    and which of them got the free place is the queue's business, already
    drawn on the board a second later.

    `skips` is `(title, refusal)` pairs and every refusal is the daemon's own
    (`PLAN_GATE_REFUSAL`, `dispatch.guard`'s words); none
    is composed here. `remaining_reason` is a token rather than prose for the
    same one-place reason — `"cap"` for the batch's own bound, `"queue_full"`
    for the store's ceiling — so an unwalked tail is explained in one voice.
    """
    moving = max(0, int(started or 0)) + max(0, int(queued or 0))
    waiting = max(0, int(already or 0))
    skips = list(skips or [])
    left = len(skips)
    remaining = max(0, int(remaining or 0))
    if not moving and not waiting and not left and not remaining:
        return "nothing here to start"
    terms = []
    if moving:
        terms.append(f"{moving} started or queued")
    if waiting:
        terms.append(f"{waiting} already waiting")
    if left:
        terms.append(f"{left} left alone")
    lines = [" \u00b7 ".join(terms) if terms else "nothing here to start"]
    for title, detail in skips[:START_PROJECT_MAX_LINES]:
        name = str(title or "").strip() or "untitled"
        lines.append(f"{name} \u2014 {str(detail or '').strip()}")
    if left > START_PROJECT_MAX_LINES:
        lines.append(f"\u2026and {left - START_PROJECT_MAX_LINES} more")
    if remaining:
        cards = "card" if remaining == 1 else "cards"
        if remaining_reason == "queue_full":
            lines.append(f"{remaining} more Backlog {cards} left where they "
                         "were: this project's queue is full")
        else:
            lines.append(f"{remaining} more Backlog {cards} left where they "
                         "were: one press walks at most "
                         f"{max(1, int(max_cards or 1))}")
    return "\n".join(lines)


def _start_batch_report(head_title: str, waiting: int, skips: list) -> str:
    """What one press of START n TOGETHER answers, in words. Pure.

    `_start_project_report`'s rule for the batch-implement press: composed
    here, drawn verbatim by the panel. The first line names the card the
    one session opened on and how many wait behind it; then one
    `<title> — <refusal>` line per skipped card, up to
    `START_PROJECT_MAX_LINES`, then `…and k more`. `skips` are
    `(title, refusal)` pairs and every refusal is the daemon's own.
    """
    head = str(head_title or "").strip() or "untitled"
    lines = [f"{head} started · {max(0, int(waiting or 0))} waiting"]
    skips = list(skips or [])
    for title, detail in skips[:START_PROJECT_MAX_LINES]:
        name = str(title or "").strip() or "untitled"
        lines.append(f"{name} — {str(detail or '').strip()}")
    if len(skips) > START_PROJECT_MAX_LINES:
        lines.append(f"…and {len(skips) - START_PROJECT_MAX_LINES} more")
    return "\n".join(lines)


_ORDINALS = ("first", "second", "third", "fourth",
             "fifth", "sixth", "seventh", "eighth")


def _already_queued_reason(position: int, autostart: bool) -> str:
    """What a *repeat* gesture on an already-queued card answers, in words.

    `_queue_reason`'s sibling, deliberately not a reuse: the badge says why
    the card is waiting, this says that the press changed nothing and where
    the card already stands. Both are composed here, server side, so the two
    sentences about one wait can never disagree, and both honour
    `board_autostart` the same way — with the drain off nobody is coming, so
    the sentence must not promise Dark Army will start it.

    The position is the card's place in the drain's own ordering
    (`board_queue.queue_key`), never a second count: a number the drain will
    not act on is worse than no number. `MAX_QUEUED_PER_PROJECT` is 8, so the
    words run out exactly where the queue does; the numeric tail beyond that
    is defensive rather than expected.
    """
    n = max(1, int(position or 1))
    ordinal = _ORDINALS[n - 1] if n <= len(_ORDINALS) else f"{n}th"
    tail = ("Dark Army will start it when a place frees up" if autostart
            else "press Start when a place frees up")
    return f"Already queued — {ordinal} in line for this project; {tail}"


def _quoted_titles(titles, joiner: str = " and ") -> str:
    """`"X"`, `"X" and "Y"`, `"X", "Y" and "Z"` — the one way a sentence
    here names cards. Blank titles read `untitled` rather than `""`."""
    names = [f'"{str(t or "").strip() or "untitled"}"' for t in (titles or ())]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + joiner + names[-1]


def _dependency_reason(titles, autostart: bool) -> str:
    """What a card queued behind an unmet dependency says, in one sentence.

    `_queue_reason`'s sibling for the other reason a queued card waits: not a
    full project but a card that has to finish first, named by title — the
    person linked those cards and is owed the name, where the slot rule has
    no single holder to name. Composed here for `_queue_reason`'s reason: the
    refusal a press answers with and the badge the card wears a frame later
    are one sentence, drawn verbatim by both clients.

    The same promise head as `_queue_reason` (with the drain off nobody is
    coming, so it says "press Start"), then **"once … done"** rather than
    "when … finishes": `when` is `_queue_reason`'s word for a place freeing,
    and a different word keeps the two waits apart on the card. It never
    begins with the plan gate's or the stale-copy refusal's opening words —
    both clients match those by prefix.
    """
    head = ("Queued — Dark Army will start it" if autostart
            else "Queued — press Start")
    names = [str(t or "").strip() for t in (titles or ())]
    if not names:
        return f"{head} once the cards it waits on are done"
    verb = "is" if len(names) == 1 else "are"
    return f"{head} once {_quoted_titles(names)} {verb} done"


def _dependency_line(entries) -> str:
    """`Waits on: "X" (done) · "Y" (not yet)` — a card's dependencies as the
    tile and the card screen draw them, verbatim, on both clients.

    One word per dependency, and the word is the met rule
    (`board_queue.dependency_met`) spelt out: `done` for a card in Done,
    `check pending` for one finished and waiting only on a person's manual
    check (met, and the reason it is met is worth seeing), `not yet`
    otherwise. `""` for none, so the line is absent rather than empty.
    """
    parts = []
    for entry in entries or ():
        title = str((entry or {}).get("title") or "").strip() or "untitled"
        if str(entry.get("column_name") or "") == "done":
            word = "done"
        elif entry.get("met"):
            word = "check pending"
        else:
            word = "not yet"
        parts.append(f'"{title}" ({word})')
    if not parts:
        return ""
    return "Waits on: " + " · ".join(parts)


def _dependents_line(titles) -> str:
    """`Unblocks: "A" · "B"` — the cards waiting on this one. `""` for none,
    so the line is absent rather than empty."""
    names = [f'"{str(t or "").strip() or "untitled"}"' for t in (titles or ())]
    if not names:
        return ""
    return "Unblocks: " + " · ".join(names)


# The pause between consecutive keystrokes when answering an AskUserQuestion.
# Digits (and digit-to-submit-keys / the trailing Enter) must be separate
# `sendText` round-trips with air between them: in one write the client can
# see `"12"` as a single key event whose `key.length` is not 1, and the
# digit branch of its select handler (`/^[0-9]$/`) never fires. See
# `answer_question`.
QUESTION_KEY_GAP_SECONDS = 0.12

# Pick-one Select, Claude Code 2.1.261 (2026-09-05): a digit 1–9 selects
# option N by absolute index (`/^[0-9]$/`, onChange → set-answer with
# shouldAdvance: true) and advances. Enter selects the *focused* option.
# There is nothing to type after the digit — the old `"\r"` landed on the
# next question and chose its first option. A several-question dialog still
# ends on a summary (Submit focused); `_type_answer_burst` keeps that
# trailing Enter. A one-question dialog submits from the digit and has
# no summary, so a spare Enter would land on the prompt line.
# A CLI bump re-reads this handler before trusting either tuple.
PICK_ONE_SUBMIT_KEYS: tuple = ()

# The pick-one dialog has a **second layout**, chosen by the client when any
# option carries a `preview` (and the question is not multi-select): the
# option list on the left, the focused option's preview on the right, a
# notes line under it. Read out of the same 2.1.261 handler: there a digit
# 1–9 only **moves focus** to option N (`Eae(SMo)` — no onChange), and Enter
# selects the *focused* option (`zRt(jf)`), which is the onChange that
# submits a one-question dialog or advances a longer one. So on that layout
# the digit alone did nothing visible — the row stayed `waiting` and every
# surface offered the same buttons again. The hook reports the layout as
# `has_preview` (any option whose `preview` is a non-empty string, which is
# the client's own `options.some(o => o.preview !== undefined)` test), and
# the burst follows the digit with this Enter on exactly those questions.
PREVIEW_PICK_ONE_SUBMIT_KEYS = ("\r",)


def _dialog_has_summary(questions: list) -> bool:
    """Whether the client ends this dialog on its "Review your answers" screen.

    The client's own rule, restated: the screen is skipped only for a single
    pick-one question (Claude Code 2.1.261, `hideSubmitTab`). Several
    questions, or one multi-select question, end there — and that screen
    takes one Enter, which `_type_answer_burst` sends last. A preview-layout
    single question submits from its Enter like any other pick-one and has
    no summary either.
    """
    if len(questions) != 1:
        return True
    first = questions[0] if isinstance(questions[0], dict) else {}
    return first.get("multi_select") is True

# The keystrokes that submit a **multi-select** AskUserQuestion once its digits
# have toggled the picks: Up, PageDown, PageDown, Down, Enter.
#
# Read out of the installed client, **Claude Code 2.1.261**, and the reading
# is the whole safety case — a CLI bump re-reads the widget before trusting
# this. A `multiSelect: true` question is drawn by a *different* widget from
# the pick-one select: a tick-box list ending in an "Other" free-text item,
# with a Submit/Next button under it ("Next" on every question but the last,
# "Submit" on the last). Its key handler, as read:
#   - a digit 1–9 **toggles** option N by absolute index; focus does not move;
#   - Space toggles the *focused* item;
#   - **Enter toggles the focused item** unless the Submit button is focused,
#     in which case it submits the ticked set — a bare Enter never submits;
#   - Down (or `j`) from the **last item** focuses Submit; a second Down while
#     Submit is focused leaves the widget altogether (`onDownFromLastItem`);
#   - Up (or `k`) while Submit is focused returns focus to the last item;
#     otherwise it moves focus up, wrapping first → last;
#   - PageDown moves focus forward by the visible count **clamped to the last
#     item**, so it is an absolute jump to the last item from anywhere;
#   - the trailing "Other" input is the last item; while it is focused digits
#     are typed into its box rather than toggling, but Down and Enter still
#     reach the handler.
# So the old `digit, Enter` toggled option N, then toggled option 1 (the
# initially focused item), submitted nothing, and the dialog stayed up. The
# sequence below, typed *after* the digits (which go first, while focus is
# still on a plain option row), is:
#   Up        — clears a Submit focus the human may have left behind; harmless
#               anywhere else (focus moves one row, and nothing here depends
#               on which row);
#   PageDown  — an absolute jump to the last item (the "Other" input);
#   PageDown  — a no-op at the end, kept as a second absolute clamp;
#   Down      — one Down from the last item focuses Submit. **Never a second
#               Down**: with Submit focused it leaves the widget;
#   Enter     — submits the ticked set. "Next" advances a multi-question
#               dialog to its next question; "Submit" on the last question
#               reaches the "Review your answers" summary screen.
# `_type_answer_burst` types every element in this order with
# `QUESTION_KEY_GAP_SECONDS` between them. Do not reorder the sequence.
#
# **The summary screen is reached by a one-question multi-select dialog
# too.** The client skips it only where the dialog is a single *pick-one*
# question (`hideSubmitTab = questions.length === 1 && !multiSelect`, and the
# pick-one onAnswer submits straight from the digit on that shape). Every
# other shape — several questions, or one multi-select — ends on that screen,
# a two-item select ("Submit answers" focused, "Cancel") that one Enter
# confirms. `_type_answer_burst` therefore sends its trailing Enter on the
# client's own rule, `_dialog_has_summary`, and not on the question count:
# gating it on the count alone left a one-question multi-select dialog
# standing on "Ready to submit your answers?" while the row stayed `waiting`
# and both surfaces offered the same tick boxes again.
MULTI_SELECT_SUBMIT_KEYS = ("\x1b[A", "\x1b[6~", "\x1b[6~", "\x1b[B", "\r")

# The removal reasons that mean the session is *gone*, as opposed to merely
# dropped from our bookkeeping. Only these stamp `_grok_ended` — see
# `_forget_session`. "evicted" is the one that must not: it is wall-clock
# staleness, which a live tab reaches simply by sitting quiet for five minutes.
# "closed" qualifies precisely because the tab is gone — `_close_session_terminal`
# disposed it, process and all — which is exactly the set's admission test;
# without the stamp `_live_grok_records` would resurrect the row from the
# roster file the dead id is still listed in.
GROK_END_REASONS = frozenset({"ended", "stopped", "cleared", "closed",
                              "no process"})

# Leader connect is a loop-owned watch, not a hook side-effect. Seconds,
# not events — a failed connect backs off inside LeaderClient.
LEADER_WATCH_SECONDS = 2.0

# Statusline messages arrive on every assistant message, across every agent. The
# metrics they carry (cost, context %) are worth showing but not worth a
# transcript re-parse each time.
METRICS_PUSH_INTERVAL = 3.0

# How long `_reconciled_categories` trusts the Grok and Codex rosters it last
# refreshed. Those refreshes glob and stat every Codex rollout, re-parse changed
# journals and walk the process table — and they run on the event loop, called
# from every agents snapshot, the liveness sweep and *every statusline message*
# (~1/s per agent). Within this window the last read is reused; a direct call to
# either `_refresh_*_records` still refreshes unconditionally, because the few
# sites that make one (the stop path, the liveness sweep) are asking on purpose.
ROSTER_REFRESH_INTERVAL = 2.0

# Shown for a session with no name of any kind. Deliberately not the session id
# and not the CLI's default slug: both look like names without being one, and the
# project is already its own column.
UNNAMED_SESSION = "New session"

# The two values shared by a card's `link_state` and its `refine_state` that
# mean an assistant is on that card right now. Deliberately *not*
# `board_queue.CLAIMING_LINK_STATES`, whose meaning is "claims a parallel
# slot" — a refinement does not, and this is asking a different question.
_ACTIVE_CARD_LINKS = ("dispatching", "live")

# How often the agent snapshot is re-pushed with nothing else prompting it, so
# names that appear later and idle counters that keep running stay current.
SNAPSHOT_REFRESH_SECONDS = 10.0

# How often `_project_facts_snapshot` re-reads history. These numbers move
# slowly (all-time counts, last-active to the minute), and a live-project set
# change bypasses the floor so a new tab does not wait a minute for its facts.
PROJECT_FACTS_INTERVAL = 60.0

# How often `_power_source_snapshot` re-runs `pmset -g batt`. A minute is the
# phone's own full-resync floor, and the reading is quantised (whole percent,
# a word for the state, no time estimate), so it is never news on its own
# clock — only when the battery actually moved.
POWER_SOURCE_INTERVAL = 60.0

# The two hooks whose card is the daemon's stock "Waiting for input" — the same
# pair the parked-turn suppression in _handle_message reasons about, and the one
# kind of card the waiting hysteresis is allowed to withhold: a StopFailure is
# an API error the human still wants to see the moment it happens.
PARKED_CARD_HOOKS = ("Stop", "Notification")

# What a subagent's own hook event is allowed to do to its parent's row.
#
# Grok fires a child's PreToolUse / PostToolUse / UserPromptSubmit / SessionEnd
# *inside the child*, stamped with the child's session id and — verified on a
# live fleet — no `parentSessionId` at all, which is why `protocol.py` cannot
# answer this and `_parent_of_child` has to. A child's tool call is the parent's
# tree working, so it is redirected: that is also what keeps a parent off the
# staleness evictor while its children do the work (the evictor's own comment
# assumes it — "active subagents refresh last_event via PreToolUse on the parent
# session" — and for Grok that was simply not true). Everything else a child
# fires is about the child's own lifetime and is dropped: its SessionEnd is not
# the parent ending, its UserPromptSubmit is the spawn prompt rather than a
# human typing, and its Stop would raise a "needs you" card for a turn nobody
# is waiting on.
CHILD_WORK_EVENTS = frozenset({
    "tool_use", "tool_done", "permission", "tool_failed", "compact",
})

# The three routers under `_handle_message`, and what each answers for.
#
# Channels speak `type`, hooks speak `event`. `CHANNEL_MESSAGE_TYPES` is the
# two channel envelopes plus the five board verbs that ride a `tools/call`
# and need a *reply*; the statusline reply travels with them because it too
# is answered rather than fire-and-forget. `LIFECYCLE_EVENTS` is everything
# the installed notify script emits about a session's own life — the vocabulary
# of `protocol.py`. A message wearing neither goes to `_route_control`, which
# today takes the same tail an unrecognised event always took.
CHANNEL_MESSAGE_TYPES = (
    "channel_attach", "channel_permission_request",
    "board_card_request", "board_close_request", "board_attach_request",
    "board_attach_report_request",
    "board_manual_request", "board_answer_request",
    # The two knowledge verbs. Answered, like the board verbs and for the same
    # reason: they ride a `tools/call` and an unanswered id is a session
    # waiting for ever.
    "knowledge_read_request", "knowledge_write_request",
    # Mission Control asking for a card to be started. Answered like the
    # board verbs; it starts nothing (`BoardVerbsMixin.ask_start`).
    "board_start_ask_request",
    # A batch session moving on to its next card. Answered like the board
    # verbs; it binds a card the person already chose and starts nothing
    # (`BoardVerbsMixin.advance_batch_by_session`).
    "board_next_request",
)
LIFECYCLE_EVENTS = frozenset({
    "session_start", "add", "dismiss", "compact",
    "tool_use", "tool_done", "tool_failed", "permission",
    "subagent_start", "subagent_stop",
    # The broker's two verbs. Lifecycle rather than channel messages because
    # they arrive from the installed hook script about a session's own life —
    # but unlike the rest of that stream they are *answered*, which the
    # routers already allow for (they return Optional[dict] and
    # `socket_server` writes any dict back).
    "permission_ask", "permission_poll",
})

# How often the daemon asks VS Code which session the user is actually looking
# at, and how long that answer is trusted for. The poll only runs when there is
# somebody it could suppress — a session already holding a card or blocked on a
# human — so the usual cost is nothing at all. The trust window is longer than
# the poll so one slow answer does not un-suppress a window nobody left; it is
# short enough that walking away re-arms the alerts within a few seconds.
FRONTMOST_POLL_SECONDS = 4.0
FRONTMOST_TRUST_SECONDS = 15.0
# How long a fresh work report's banner may wait for a frontmost reading
# taken after its row went quiet (`_report_hold`): one poll to be asked, one
# to spare, and the slow refresh that re-evaluates. Past it, fail open.
REPORT_FRONTMOST_WAIT_SECONDS = 2 * FRONTMOST_POLL_SECONDS + SNAPSHOT_REFRESH_SECONDS

# The phone leg's two always-on gates (22 Sep 2026, S6 and S7 in
# the false-buzz investigation of 20 Sep 2026; stated
# in `docs/transport-contract.md`, *The phone leg is filtered, gated and
# held*). No preference reaches either. A phone buzz is withheld when the
# Mac was touched inside `MAC_PRESENT_SECONDS` — the person is at the desk,
# where the panel and the strip already say the same — and an unreadable
# reading sends. A `card`-rule alert is held `PUSH_GRACE_SECONDS` first and
# dropped if its card went (answered, dismissed) or its session was muted
# inside the grace. Both are read at call time, so a test can set them.
MAC_PRESENT_SECONDS = 60.0
PUSH_GRACE_SECONDS = 30.0

# How long the docked sidebar's "I am on screen" reading is trusted. The panel
# heartbeats it every 5s while expanded, so three missed beats fit inside the
# window before banners fail open — the same shape as the frontmost poll above
# against its trust. The TTL is one of three independent safeties on this flag
# (with the EOF clear in panel_process and the clear on undocking): a dead or
# hung panel costs at most this many seconds of silence, never more.
PANEL_VISIBLE_TRUST_SECONDS = 15.0

# One persisted statusline reading per session per interval. The source ticks
# on every assistant message; none of what it carries moves that fast.
METRIC_SAMPLE_INTERVAL = 60.0

# Retention sweep cadence. Daily would do; hourly keeps it from being a
# long-running app's forgotten chore.
HISTORY_PRUNE_INTERVAL = 3600.0

# How often transcripts are re-scanned for per-turn detail. Unchanged files cost
# one stat each, so this is cheap; the first pass on a large backlog is not, which
# is why it runs in the background rather than blocking startup.
TRANSCRIPT_SCAN_INTERVAL = 900.0


# Synthesis rides the user's own Claude Code subscription, and this app is the
# thing that shows them how much of it is left. Spending the last of it to draw a
# chart of how little is left would be a poor trade, so a run above this
# utilisation is skipped and retried on the next tick.
THEME_LIMIT_SKIP_PCT = 90.0

# Claude Code's default display name is the project directory plus two hex
# characters — "shop-front-00", "demo-app-63", "t-f3". `claude agents --json`
# reports it as `name`, indistinguishable by type from a real one.
_DEFAULT_SLUG_SUFFIX = re.compile(r"-[0-9a-f]{2}$", re.IGNORECASE)


def _is_default_slug(name: str, project: str) -> bool:
    """True if `name` is Claude Code's auto-generated display name for `project`.

    Matched against the project rather than by shape alone, so a session someone
    genuinely called "release-4f" keeps its name."""
    if not project:
        return False
    match = _DEFAULT_SLUG_SUFFIX.search(name)
    if not match:
        return False
    return name[: match.start()].casefold() == project.casefold()

# How long a session may go silent before it is presumed dead. Startup prune
# and the runtime loop both read this; the menu bar may overwrite it at
# launch with a stored press (600 / 1800). 0 (the old picker's Never) is
# not a timeout — `set_session_timeout` turns it into this default.
DEFAULT_SESSION_STALENESS_SECONDS = 300.0

# How long an agent that stopped working stays in the panel's "Recently finished"
# section. Long enough to still be there when you come back from whatever you did
# while it ran, short enough that the section is about *this* stretch of work and
# not a second graveyard — the abandoned bucket already covers the long tail.
FINISHED_RETENTION_SECONDS = 30 * 60.0


# How long a live session must be quiet before it reads as finished rather than
# merely sleeping — `session_stats.FINISHED_IDLE_GRACE_SECONDS`, the one number
# `alerts.py` also reads for a work report's freshness.
FINISHED_IDLE_GRACE_SECONDS = ss.FINISHED_IDLE_GRACE_SECONDS

# Bound on the tombstone store. Retention already bounds it in practice; this is
# the backstop for a machine churning through sessions faster than they expire.
MAX_FINISHED_RECORDS = 50


@runtime_checkable
class DaemonObserver(Protocol):
    # Receives a snapshot list of the active notifications (see
    # BobDaemon._notification_snapshot); len() of it is the count.
    def on_notification_change(self, notifications: list[dict]) -> None: ...
    # Receives the current per-category counts driving the menu-bar strip — see
    # BobDaemon._activity_counts for the bucketing rules:
    #   working_count   — sessions actively working (state working/thinking, or a
    #                     live subagent)
    #   idle_count      — sessions that are idle
    #   attention_count — sessions that need the human (pending notification card,
    #                     or waiting/error state; confused only beside a card)
    #   subagent_count  — total live subagents across all sessions (shown as the
    #                     "(+M)" suffix on the working group)
    # The three session buckets are disjoint, so they always sum to the number of
    # tracked sessions. Optional — default no-op so observers that don't care need
    # not implement it.
    def on_activity_change(
        self, working_count: int, idle_count: int, attention_count: int,
        subagent_count: int,
    ) -> None: ...
    # Receives the rich per-agent view (BobDaemon.detailed_snapshot) for the
    # expandable menu: {"running": [...], "sleeping": [...], "waiting": [...]}.
    # Pushed (throttled) on structural state changes. Optional.
    def on_agents_change(self, snapshot: dict) -> None: ...
    # Receives alerts that have earned an interruption (see alerts.AlertPolicy),
    # as dicts, for an observer that can actually post them. Delivered once and
    # then forgotten — unlike every other callback here, which re-sends current
    # state, this one is an event. Optional.
    def on_alerts(self, alerts: list[dict]) -> None: ...
    # Receives the Kanban board (BobDaemon._board_snapshot) whenever a card
    # changes or a reconcile moves one. Optional — default no-op, like
    # on_activity_change.
    #
    # A new callback rather than a fifth argument on `on_activity_change`, and
    # that is deliberate: that signature is positional, implemented by both the
    # API server and the menu bar and asserted by tests, so widening it would
    # make every observer learn about the board in order to go on counting
    # sessions.
    def on_board_change(self, board: dict) -> None: ...
    # Receives the session id a VS Code window asked Dark Army to show — the reverse
    # of Jump, sent by the extension's "Dark Army: Show this session" command (see
    # BobDaemon.reveal_panel_for_terminal). `""` means the terminal matched no
    # session and the panel should simply come forward. Optional, like
    # on_board_change: only the menu bar (which owns the panel process) can act
    # on it, and the API server must cost nothing to leave it unimplemented.
    def on_panel_reveal(self, session_id: str) -> None: ...
    # Receives one preference a phone has asked the Mac to change: the key
    # (a member of `PHONE_PREFERENCES`) and the value the wire carried. The
    # daemon stores nothing of its own here — `preferences.json` belongs to
    # the menu-bar app and one writer is the whole point — so this is a
    # *request*, handed over rather than applied. Optional, exactly like
    # `on_panel_reveal`: only the menu bar can act on it, and the API server
    # must cost nothing to leave it unimplemented. `_observers_implementing`
    # is therefore also the published "can this Mac honour it at all".
    def on_preference_request(self, key: str, value) -> None: ...


#: The app bundle a daemon can run from, as a whole path component.
_OUR_BUNDLES = ("Dark Army.app",)
#: Its frozen executable's name.
_OUR_EXECUTABLES = ("Dark Army",)
#: What a daemon runs as from source (`python -m <module>`), exact modules
#: only: the pty broker and the relay bot are ours but are not daemons.
_OUR_DAEMON_MODULES = ("dark_army_menubar", "dark_army_menubar.app",
                       "dark_army_daemon", "dark_army_daemon.daemon")


def _looks_like_bob_daemon(proc: "psutil.Process") -> Optional[bool]:
    """Whether this process is plausibly a Dark Army daemon.

    Read before `_stop_existing_daemon` signals anything: the pid file is just
    a number, and after a crash the number is routinely somebody else's — a
    recycled pid pointing at a browser is not a daemon to take over from.

    **Only the executable and exact argv elements count, never free text.**
    A command line is words anybody can type: an agent's prompt quoting the
    product's name, an editor or `tail` holding a file under `~/.dark-army`.
    Ours is an executable inside one of `_OUR_BUNDLES` (or named one of
    `_OUR_EXECUTABLES`), or an interpreter running `-m` one of
    `_OUR_DAEMON_MODULES` as two adjacent elements.

    Three answers, because unreadable is neither: `True` (ours to signal),
    `False` (provably a stranger — the pid file is stale), `None` (identity
    we could not read — not ours to signal, and not proof the file is stale
    either).
    """
    try:
        argv = [a for a in (proc.cmdline() or ()) if isinstance(a, str)]
        exe = proc.exe() or ""
    except (psutil.Error, OSError):
        return None
    parts = Path(exe).parts if exe else ()
    if any(bundle in parts for bundle in _OUR_BUNDLES):
        return True
    if parts and parts[-1] in _OUR_EXECUTABLES:
        return True
    for i, arg in enumerate(argv[:-1]):
        if arg == "-m" and argv[i + 1] in _OUR_DAEMON_MODULES:
            return True
    return False


def _pid_on_file() -> Optional[int]:
    """The pid the daemon's pid file names, or None without a readable one."""
    try:
        return int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def _decline_to_signal(pid: int, verdict: Optional[bool]) -> None:
    # A number we cannot vouch for gets no signal. Unreadable identity keeps
    # the file (it proves nothing either way); a proven stranger means the
    # file is stale and outlived its daemon, so it goes.
    if verdict is None:
        logger.warning("PID file names %d but its identity is unreadable — "
                       "signalling nothing", pid)
        return
    logger.warning("PID file names %d but that process is not Dark Army — "
                   "removing the stale pid file, signalling nothing", pid)
    try:
        PID_PATH.unlink()
    except OSError:
        pass


def _stop_existing_daemon() -> bool:
    """Ask the daemon the pid file names to quit, and wait up to 3s for it.

    True once it is gone, including when it already was. The process is
    identified before anything is sent (`_looks_like_bob_daemon`): pids are
    reused, and a leftover number now naming some other program must get no
    SIGTERM at all.
    """
    pid = _pid_on_file()
    if pid is None:
        return False
    try:
        holder = psutil.Process(pid)
        verdict = _looks_like_bob_daemon(holder)
        if verdict is not True:
            _decline_to_signal(pid, verdict)
            return False
        holder.terminate()
        holder.wait(timeout=3.0)
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False
    except psutil.TimeoutExpired:
        logger.warning("The daemon on PID %d was still running 3s after SIGTERM", pid)
        return False
    return True


def _lock_now(lock: FileLock) -> bool:
    try:
        lock.acquire(blocking=False)
    except FileLockTimeout:
        return False
    return True


def _exit_with(reason: str, status: int) -> None:
    print(reason, file=sys.stderr)
    sys.exit(status)


def _acquire_lock(takeover: bool = False) -> FileLock:
    """Hold the one-daemon-per-account lock, or leave.

    `takeover` is the menu-bar app's start: a daemon already holding the
    lock is stopped and the lock taken, and the process exits 1 if it still
    cannot be. Without it (a headless start) the running daemon wins and
    this one exits 0. The caller releases the returned lock at shutdown.
    """
    ensure_state_dir()
    lock = FileLock(str(LOCK_PATH))
    if _lock_now(lock):
        return lock
    if not takeover:
        _exit_with("Another Dark Army daemon is already running", 0)
    logger.info("Another daemon holds the lock; stopping it to take over")
    _stop_existing_daemon()
    if not _lock_now(lock):
        _exit_with("The daemon lock was still held after stopping its holder", 1)
    return lock


#: Which hook events carry a tool name, and which a subagent id, for the
#: one log line each hook message leaves.
_EVENTS_NAMING_A_TOOL = frozenset({"tool_use", "tool_done", "permission", "tool_failed"})
_EVENTS_NAMING_A_CHILD = frozenset({"subagent_start", "subagent_stop"})


class _HookStep(NamedTuple):
    """One event's inputs to `BobDaemon._update_session_state`'s steps."""

    hook: str
    agent_id: str
    tool_name: str
    parked: bool
    quiet: bool
    subagent_type: str
    now: float
    now_mono: float


class BobDaemon(BoardVerbsMixin):
    def __init__(
        self,
        observer: Optional["DaemonObserver"] = None,
        headless: bool = True,
        sessions_path: Optional[Path] = None,
        identities_path: Optional[Path] = None,
        session_timeout: Optional[int] = None,
    ):
        self._socket = SocketServer(on_message=self._handle_message)
        self._active_notifications: dict[str, dict] = {}
        # Channels, keyed by the loopback port each one listens on — the only
        # thing about a channel that is guaranteed unique, since two sessions in
        # one repo share a cwd and a pid can be reused. See channel_server.py.
        self._channels: dict[int, dict] = {}
        # Tool-approval prompts a channel has relayed to us, keyed by the id
        # Claude Code issued. This is the one piece of state in the daemon that
        # a *human* is expected to answer, and it is deliberately not a
        # notification card: a card is dismissible and this is not — dismissing
        # a permission prompt would leave the session blocked with nothing on
        # screen to say why.
        self._permission_requests: dict[str, dict] = {}
        # The question each session is currently stopped on, as the last
        # enrichment pass read it — session_id -> the same dict the snapshot
        # publishes ({"text", "options", "header", "id"}). Kept so
        # `answer_question` can check a verdict against the freshest reading
        # without re-parsing a transcript inside an action handler, which is
        # the wrong place for transcript I/O. Written from the executor by
        # `_enrich_agent_stubs` (whole values per key, never mutated in
        # place), read on the loop — the `_frontmost_pids` pattern.
        self._questions: dict[str, dict] = {}
        # Sessions with an answer burst currently being typed. A multi-question
        # dialog takes up to MAX_QUESTIONS digit+Enter pairs (~1s at
        # QUESTION_KEY_GAP_SECONDS), and a second burst landing inside the
        # first would interleave keystrokes on one input line — so
        # `answer_questions` holds a per-session slot here for the life of the
        # burst and refuses a second press in words. Touched only on the loop.
        self._answering: set[str] = set()
        self._codex_reply_attempts: dict[str, tuple] = {}
        # The same thing, learned a different way and *earlier*: the question a
        # session is blocked on as its PreToolUse hook reported it. The
        # transcript is the better source — it carries the real tool_use id —
        # but it is not a timely one, because Claude Code writes the asking
        # turn only when the answer arrives (see
        # `session_stats.question_from_tool_input`). So the row spent the whole
        # wait saying "Waiting" and nothing else, on exactly the sessions that
        # had a question with named options to draw. Held on the loop, where
        # the hook events land, and read by the enrichment pass as the fallback
        # under `stats.question`. Cleared by anything that says the dialog is
        # gone: the PostToolUse that answers it, any other tool call, a typed
        # prompt, the session ending.
        #
        # And **persisted**, alongside the session states themselves, which is
        # not the belt-and-braces it looks like. Every other in-memory map here
        # can be rebuilt from a source that outlives the process; this one
        # cannot, because the transcript gains the asking turn only when the
        # answer does. A menu-bar restart — which the panel's own Rebuild row
        # invites, and an auto-update performs unasked — therefore erased the
        # single thing the blocked row had to say, and it stayed erased for as
        # long as the human took to walk past: measured on a live session, a
        # question raised at 00:23:19 and still on screen at 00:31 as the prose
        # of the turn *before* it, because the app restarted at 00:26:40.
        # Restored in `_restore_pending_questions` under two conditions, since
        # a question is only news while its dialog is up.
        self._pending_questions: dict[str, dict] = {}
        # session_id -> (transcript mtime, derived session title); mtime-invalidated
        self._ai_title_cache: dict[str, tuple[float, str]] = {}
        # Stable nicknames. Loaded once here rather than per snapshot: the file is
        # the daemon's own, and re-reading it on the enrichment path would let a
        # partial write rename every agent at once.
        self._identities = IdentityStore(identities_path)
        # What the last snapshot actually put on screen, session -> nickname.
        # Only ever read to settle a two-claimant tie in favour of the row
        # somebody may be reading; see _assign_nicknames.
        self._nicknames_shown: dict[str, str] = {}
        # Beside it, the `name` and `started_at` each row wore in the last
        # enriched snapshot — what the event log's finish line reads, since
        # by then the row is gone. Replaced whole per snapshot, never mutated.
        self._names_shown: dict[str, str] = {}
        self._starts_shown: dict[str, float] = {}
        # The diary (`event_log.EventLog`), opened in `run()` beside
        # `_history`; None until then and every hook is a no-op on None.
        # `_logged_starts` is the set of sessions whose start line has been
        # written — a finish is logged only for one of those, and the id is
        # popped in `_log_session_end` (off `_record_finished`, the seam every
        # finish shares) so a returning session logs again. Seeded from the
        # diary in `run()` so a restart writes no false start.
        self._event_log = None
        # The phone doors' access log (`access_log.AccessLog`), opened in
        # `run()` beside the diary on the same terms; every recorder is a
        # no-op on None. The detector is pure and in memory, so it is built
        # here and never fails.
        self._access_log = None
        self._burst_detector = access_log.BurstDetector()
        # The phone's link timing (`link_timing.LinkTimer`): pure, in
        # memory, fed on the loop by `note_link_timing` from both sealed
        # doors; one closed ten-minute window lands on the access log as
        # a `timing` line. It never reaches the detector above.
        self._link_timer = link_timing.LinkTimer()
        # The buzz ledger (`buzz_ledger.BuzzLedger`), opened in `run()` on the
        # same terms; `_ledger_buzz` records nothing on None.
        self._buzz_ledger = None
        # The open alert a fold lands in — id, door, peer, the running
        # `count` and a bounded `peers` set — set by `_raise_access_alert`,
        # moved by `_fold_access_alert`, read on the loop alone. The
        # published totals come off the journal's newest `burst_more` line,
        # never off this, so `security_snapshot` reads only the store.
        self._access_fold: Optional[dict] = None
        # `_record_access` fires one task per knock, so a flood is
        # concurrent: the detector's answer and the append it leads to are
        # taken under this lock, or a sibling knock resuming inside the
        # raise's executor hop would find `_access_fold` unset and drop its
        # fold. The refusal append stays outside it.
        self._access_alert_lock = asyncio.Lock()
        self._decisions = None
        self._logged_starts: set[str] = set()
        # The same two strings the panel shows, pushed at the session's own
        # terminal tab. Reads the snapshot and nothing else — see terminal_title
        # for why the daemon owns the title rather than a launcher or the hook.
        self._titles = terminal_title.TitleWriter()
        # Interrupts already decided on, kept as a record. The daemon does not
        # deliver: it runs on a thread with no claim on AppKit, and a decision is
        # testable where a banner is not.
        self._alerts: list[dict] = []
        # The ones not yet handed to a deliverer. The daemon still does not post
        # anything itself — it cannot; it hands these to observers that can, and
        # the menu-bar process is the one with a bundle identity (the panel
        # binary, living in Contents/Resources, has none, so
        # UNUserNotificationCenter.current() would trap there).
        self._undelivered: list[dict] = []
        # The phone legs in flight (`_start_phone_leg`): one loop task per
        # drain, discarded by its own done-callback, cancelled and awaited by
        # `_shutdown` before the ledger closes. Loop thread only.
        self._phone_leg_tasks: set = set()
        # The last Live Activity content state each phone was sent, keyed on
        # device id — or the string "ended" once an end has landed, so a
        # phone whose card is down is not sent an end per frame. Memory
        # only, loop thread only; a fresh token registration pops the entry
        # (`forget_live_activity`), which is what lets a new activity start
        # on a clean slate after an end killed the old token.
        self._live_activity_last: dict[str, object] = {}
        # The last body each phone was *sent* and how it fared — a refused
        # body (a 4xx, Apple's refusal, an undeployed route) is never sent
        # again until the picture changes; an unreachable one (no answer, a
        # 5xx) is retried no sooner than `LIVE_ACTIVITY_RETRY_SECONDS` — so
        # a dead token or an old mailbox costs one bucket token per distinct
        # state, never one per pass, and the buzz sharing `_push_bucket` is
        # never starved. Value: (body, outcome, monotonic stamp).
        self._live_activity_attempt: dict[str, tuple[dict, str, float]] = {}
        # The body each phone has a POST out for right now: a slow mailbox
        # spanning several passes is one task, never one per pass.
        self._live_activity_inflight: dict[str, dict] = {}
        # Per device, a counter stepped by every send's creation and by
        # `forget_live_activity`; a send captures it before its await and
        # discards its answer if it moved — an `end` that lands after the
        # phone registered a new activity's token must not mark the new
        # activity "ended", and an older update landing after a newer one
        # was sent must not become the remembered state.
        self._live_activity_generation: dict[str, int] = {}
        # When the last live-card POST for this device landed, monotonic.
        # A shape-2 change that is only cost or tokens waits
        # `LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS` after this stamp; a
        # count, the face or an end does not. Popped with the other
        # per-device memory by `forget_live_activity`.
        self._live_activity_sent_at: dict[str, float] = {}
        # The fleet's burn per hour: growth of every live row's cost and
        # tokens over the last hour. Observed by both composers — the
        # live card here and `/api/state`'s `fleet_figures` — which is
        # harmless: the same snapshot twice adds nothing.
        self._burn_meter = fleet_figures.BurnMeter()
        # The pids VS Code last said are in the terminal on screen, and when it
        # said so. Written on the loop thread by `_frontmost_checker`, read from
        # the executor by `_alert_suppressed` — a set and a float, replaced
        # wholesale rather than mutated, which is why neither needs a lock.
        self._frontmost_pids: set = set()
        self._frontmost_at: float = 0.0
        # The sessions that reading was asked about, and the sleeping rows
        # whose fresh work report still waits on one (`alerts.report_pending`,
        # written by the executor after each evaluation) — both replaced
        # wholesale, the same pattern. A report is bannered only once a
        # reading asked about *it* was taken after it went quiet.
        self._frontmost_asked: set = set()
        self._report_candidates: set = set()
        # When this daemon started, for the alert policy: a work report whose
        # row went quiet before a restart is not announced again.
        self._started_wall: float = time.time()
        # Whether the panel's docked sidebar is on screen, and when it last
        # said so. Written on the panel's stdout reader thread (in the menu-bar
        # process, via note_panel_visible), read from the executor by
        # `_alert_suppressed` — two scalars, replaced rather than mutated, so
        # no lock; the same pattern as `_frontmost_pids` above.
        self._panel_visible: bool = False
        self._panel_visible_at: float = 0.0
        # Which session's Dark Army-owned terminal the panel is drawing, and when
        # it last said so. `_panel_visible`'s pattern: written on the panel's
        # stdout reader thread by `note_panel_terminal`, read from the
        # executor by `_alert_suppressed` under `FRONTMOST_TRUST_SECONDS` —
        # the rebuilt half of frontmost suppression for a terminal VS Code
        # cannot see.
        self._panel_terminal_sid: str = ""
        self._panel_terminal_at: float = 0.0
        self._running = True
        self._shutdown_event = asyncio.Event()
        self._lock: Optional[FileLock] = None
        self._observers: list["DaemonObserver"] = [observer] if observer is not None else []
        # Ground truth from `claude agents --json`, keyed by session id. Additive:
        # it enriches and extends what the hooks tell us, and never evicts.
        self._agent_records: dict[str, agents_poll.AgentRecord] = {}
        # Live Grok sessions from ~/.grok/active_sessions.json. Same role as
        # _agent_records, without forking a CLI — the file is the roster.
        self._grok_records: dict[str, grok_roster.GrokRecord] = {}
        # Recently changing Codex rollout journals. Most stay passive; the one
        # safe control subset is a standalone native CLI whose exact resume
        # identity can be replayed. Shared app/IDE processes never enter it.
        self._codex_records: dict[str, codex_rollouts.CodexRecord] = {}
        self._conversations = conversation.ConversationReader()
        self._codex_navigation = MappingProxyType({})
        self._refinement_receipts: dict[str, RefinementCloseReceipt] = {}
        # A Hide suppresses one exact journal reading, never the journal itself.
        # session id -> (path, (mtime_ns, size)); an append or replacement path
        # invalidates it, and a daemon restart intentionally forgets the map.
        self._hidden_codex: dict[str, tuple[Path, tuple[int, int]]] = {}
        # Folders Dark Army has turned away for want of an enrolment key, so the
        # panel can name one and offer to enrol it. Memory only and bounded —
        # see `_note_unenrolled` for why the claimed path is corroborated.
        self._unenrolled: dict[str, dict] = {}
        self._unenrolled_other = 0
        # The first-run checklist's observations, one immutable tuple built on
        # the snapshot executor by `_observe_checklist` and only *read* by
        # `enrollment_snapshot()` — the API path starts no walk of its own.
        self._checklist_facts: tuple = ()
        # Session ids we have already ended (SessionEnd, a departed roster
        # row, a dead PID). Grok is slow to drop them from the file, and
        # re-reading the leftover would resurrect the row on the next tick.
        # Cleared once the file itself no longer lists the id.
        self._grok_ended: set[str] = set()
        # Grok sessions whose own process environment has been read for
        # its origin stamp, keyed to the pid it was read from — once per
        # session and pid (`_probe_grok_origin`). A hook message from Grok
        # carries the shared leader's stamp, never the session's.
        self._grok_origin_probed: dict[str, int] = {}
        # Runs Dark Army ended by closing the terminal.
        # sid → (monotonic, pid, create_time). pid/create_time are the Codex
        # process Dark Army closed, so a dying pid or a sibling TUI in the same cwd
        # cannot look like a resume. Honoured by `_on_agent_records` and
        # `_refresh_codex_records`. Grok already has `_grok_ended` and is not
        # given a second gate. Pruned on FINISHED_RETENTION_SECONDS — the
        # same clock as the tombstone this note exists to protect.
        self._closed_ids: dict[str, tuple[float, Optional[int], Optional[float]]] = {}
        # sid → the turn a shared-server Codex close ended. Those threads have
        # no process identity to compare, so a new turn on the thread is the
        # resume (`_codex_thread_resumed_after_close`). Lives and dies with
        # the `_closed_ids` entry beside it.
        self._closed_codex_turns: dict[str, str] = {}
        # sid → monotonic stamp of a `/low-priority` Dark Army typed and saw land.
        # Written on the loop (inside `low_priority_session`), read on the
        # executor for `can_low_priority`; a stale read is one frame of a
        # button the verb then refuses in words. Memory only — a restart
        # forgets it, and the card it answered is gone with it.
        self._low_priority_sent: dict[str, float] = {}
        # Live Grok clients of a running leader. Replaced wholesale on the
        # loop, read from the enrich executor — same pattern as
        # `_frontmost_pids`. Resident is live state, not a sessions.json key.
        # `_leader` is constructed in run(), never here: tests construct
        # BobDaemon constantly, and connecting would fork `grok`.
        self._grok_resident: frozenset[str] = frozenset()
        self._grok_leader_up: bool = False
        self._leader = None
        self._agents_poller = agents_poll.AgentsPoller(on_records=self._on_agent_records)
        # session_id -> when it entered `blocked` (see _track_blocked_since)
        self._blocked_since: dict[str, float] = {}
        # Per-session metrics from the statusline collector (cost, context window,
        # rate-limit budget) — see protocol.flatten_statusline.
        self._session_metrics: dict[str, dict] = {}
        # The last few minutes of those readings, per session, so a rule can say
        # "at this rate" — see samples.py. A few hundred bytes each, in memory,
        # and pruned in `_collect_agent_stubs` alongside `_session_metrics`.
        self._samples = samples.SampleRing()
        # Sessions that stopped working, kept for FINISHED_RETENTION_SECONDS so the
        # panel can still show what a run cost after it is over. In memory only: a
        # daemon restart legitimately forgets, and sessions.json is for recovering
        # what is *live*. See _record_finished.
        self._finished: dict[str, dict] = {}
        self._last_metrics_push: float = 0.0
        # When `_reconciled_categories` last refreshed the Grok/Codex rosters
        # (monotonic). None forces the first call to refresh.
        self._roster_refreshed_at: Optional[float] = None
        # The loop-side roster refresh in flight, if any (`_schedule_roster_refresh`).
        self._roster_refresh_task: Optional[asyncio.Future] = None
        # Steps on every roster apply; a read taken before a step is stale.
        self._roster_apply_gen: int = 0
        self._agents_push_pending: bool = False
        # The two tiers of `_push_agents_snapshot`: when the full tier last
        # ran (monotonic) and the structural key it ran on.
        self._last_full_push_at: Optional[float] = None
        self._last_structural_key: Optional[tuple] = None
        # `(monotonic stamp, registry)` for `_registry_cached`; the full tier
        # clears it so a structural change always re-reads.
        self._registry_memo: Optional[tuple] = None
        # Durable history. Opened in run(): constructing a BobDaemon (tests do
        # it constantly) must not create a database as a side effect.
        self._history: Optional[history.HistoryStore] = None
        # refresh share the same database writes, and two synthesis calls racing
        # would each pay for the other's work.
        self._last_metric_sample: dict[str, float] = {}
        self._api = ApiServer(self)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # The panel pane's last stated size per pty handle
        # (`terminal_stream_resize`), re-applied by `note_panel_terminal`
        # when the pane comes back into focus so the Mac takes its width back
        # from the phone (`terminal_phone_resize`) without restating it.
        # Pruned beside the pty reap. And the handles whose phone resizes
        # were last ignored, so the refusal is logged once per stream.
        self._panel_pane_size: dict = {}
        self._phone_resize_ignored: set = set()
        # Whether the phone listener should be up. Stashed here before the
        # loop exists (preference apply in `_start_daemon_thread`) and acted
        # on after `await self._api.start()` in `run()`. The AppKit toggle
        # writes it and schedules `start_lan`/`stop_lan` on the loop.
        self.lan_access_enabled: bool = False
        # The away path's own consent, a second key deliberately: `lan_access`
        # means "bind 19875 on the LAN", and reusing it would silently turn on
        # an internet path for everyone who already had LAN on. Same stash-
        # then-apply lifecycle as the LAN flag.
        self.remote_access_enabled: bool = False
        # The socket lane's own consent (`relay_ws.py`), a third key: started
        # only while this *and* `remote_access_enabled` are on, stopped by
        # either going off. Same stash-then-apply lifecycle.
        self.relay_ws_enabled: bool = False
        # Whether a raised alert also buzzes the paired phone through the
        # push route. A plain read-at-use bool (the `notification_sound`
        # pattern): `_push_phone_alerts` checks it per drain, so flipping the
        # switch silences the very next alert rather than the one after.
        # Rides on top of `remote_access` — no away consent, no push.
        self.phone_push_enabled: bool = True
        # The connector that polls the mailbox. Built lazily on first enable,
        # started/stopped on the loop by `set_remote_access`.
        self._relay_connector: Optional[relay_client.RelayConnector] = None
        # The socket connector beside it — one live line per paired phone to
        # the socket relay. Built lazily by `_start_relay_socket`.
        self._relay_socket: Optional[relay_ws.RelaySocketConnector] = None
        # Stepped by every `set_remote_access`; the off-apply re-reads it
        # after each await and bails when a later stash has moved it.
        self._remote_access_generation = 0
        # What was driven from outside the house: a bounded record of
        # {device_id, action, at, ok}, published in `devices_snapshot`.
        self._remote_activity: deque = deque(maxlen=50)
        # `(monotonic stamp, hosts)` — one reading of this machine's own
        # addresses, held for `PAIRING_HOSTS_TTL`. See `_current_lan_hosts`.
        self._lan_hosts_cache: tuple[float, list[str]] | None = None
        self._headless = headless
        self._sessions_path = sessions_path if sessions_path is not None else session_store.SESSIONS_PATH
        # Run state-dir creation + one-time legacy migration before the first read.
        if self._sessions_path.parent == STATE_DIR:
            ensure_state_dir()
        loaded_states = load_sessions(self._sessions_path)

        # Same clamp `set_session_timeout` uses: 0 is the old picker's Never
        # and would drop every non-waiting session on the next tick. Applied
        # *before* the prune so a stored 600 / 1800 is the number Restart
        # measures, not the five-minute default the runtime loop would then
        # honour for the rest of the process.
        try:
            timeout = int(session_timeout) if session_timeout is not None else 0
        except (TypeError, ValueError):
            timeout = 0
        if timeout <= 0:
            timeout = int(DEFAULT_SESSION_STALENESS_SECONDS)
        self._session_staleness_timeout: float = float(timeout)

        # What was saved is judged once, on the wall clock, because the
        # monotonic readings it was saved with mean nothing in this process: a
        # session silent past the staleness timeout belonged to a run that
        # ended while the daemon was down. A session waiting on the person is
        # spared, as the running sweep spares it; no card has been restored
        # yet (cards are not persisted), so that is judged on its state
        # alone. From here on every clock is monotonic, which sleep and wake
        # do not disturb.
        saved_at_most = time.time() - self._session_staleness_timeout
        self._session_states: dict[str, dict] = {
            sid: entry for sid, entry in loaded_states.items()
            if self._waiting_on_human(sid, entry)
            or entry.get("last_event", saved_at_most) >= saved_at_most
        }
        restarted = time.monotonic()
        for entry in self._session_states.values():
            entry["last_event_monotonic"] = restarted
            entry.setdefault("ask_note", "")
        # After the prune, so a question whose session did not survive it
        # goes with the session.
        self._restore_pending_questions(load_pending_questions(self._sessions_path))

        # Debounced/coalesced notification chime (see _schedule_notification_sound).
        # Muting is checked when the timer *fires*, not when it is armed, so
        # toggling the menu item takes effect on an already-pending chime.
        self.notification_sound_enabled: bool = True
        self._sound_pending: bool = False
        self._pending_sound_sessions: set[str] = set()
        self._sound_task = None  # keep a ref so the timer task isn't GC'd mid-sleep

        # Compacting a full session instead of asking you to (autocompact.py).
        # Same plain-attribute shape as the chime above, and for the same reason:
        # the toggle runs on the AppKit thread, and the policy reads the flag at
        # the moment it decides rather than holding a copy of it.
        self.auto_compact_enabled: bool = True
        # Reply by typing onto the session's own input line (`_reply_by_typing`)
        # rather than only through the channel. Same plain-attribute shape:
        # read on the loop at the moment of the reply and on the snapshot
        # executor when `reply_via` is published. Off until the menu bar says
        # otherwise, so a headless daemon replies exactly as it always did.
        self.typed_reply_enabled: bool = False
        # When the last agents snapshot went out, for the push floor above.
        self._last_agents_push: Optional[float] = None
        # Sessions the policy has just decided to compact. Filled on the
        # executor thread that builds the snapshot, drained on the loop thread
        # that can actually open a socket — the same split `_undelivered` uses,
        # for the same reason.
        self._compact_queue: list = []

        # Naming a session Claude Code no longer names (session_title.py). Same
        # executor-decides / loop-acts split as the compacts above — a title
        # costs a subprocess, which is the one thing the snapshot thread must
        # never wait on.
        # The ⋯ row is gone; a stored `session_title: false` still wins so
        # an upgrade does not start sending transcript-derived text off the
        # machine behind somebody's back. `_consider_title` copies this onto
        # the shop so a mid-run flip (tests) bites without a restart.
        self.session_title_enabled: bool = True
        self._titles_shop = session_title.TitleShop(
            # Keyed off the injected sessions path rather than STATE_DIR, so a
            # test daemon writes its titles next to its own sessions.json
            # instead of into the user's.
            path=self._sessions_path.parent / "titles.json")
        self._titles_shop.enabled = bool(self.session_title_enabled)
        self._titles_shop.load()
        # Needs you acknowledgements. Keyed off the injected sessions path
        # for TitleShop's reason: a test daemon writes beside its own
        # sessions.json instead of into the user's.
        self._inbox_acks = inbox_ack.InboxAckStore(
            path=self._sessions_path.parent / "inbox-acks.json")
        self._inbox_acks.load()
        # (session_id, opening request) the shop has just decided to name.
        self._title_queue: list[tuple[str, str]] = []
        self._title_task = None
        # How important a new card is (card_priority.py). The filer's sibling
        # in every respect — one attempt per card ever, a queue filled on the
        # executor inside `_reconcile_board` and drained on the loop, serial
        # and detached — because the score costs a subprocess and the
        # snapshot thread must never wait on one.
        self._priority_shop = card_priority.PriorityShop(
            # Keyed off the injected sessions path for the filer's reason, so
            # a test daemon writes its scores next to its own sessions.json
            # instead of into the user's.
            path=self._sessions_path.parent / "card-priority.json")
        self._priority_shop.load()
        # (card_id, title, summary, project) the shop has just decided to ask
        # about.
        self._priority_queue: list = []
        self._priority_task = None
        # What Dark Army observed a run do (`work_record.py`, the store's
        # `card_runs` verbs). Same split as the filer: the reconcile queues
        # on the executor, the loop runs the two bounded `git` calls
        # serially and detached. `_work_open_tasks` holds the baseline
        # readings taken at dispatch, which nothing awaits — a slow git must
        # never delay a terminal that has already opened. `_work_recorded`
        # is `{card_id: run_at}`, so a restart replaying the reconcile grace
        # does not collect the same run twice.
        self._work_record_queue: list = []
        self._work_record_task = None
        self._work_open_tasks: set = set()
        # The auto-starts a landed plan asks for (`start_when_planned`).
        # `_work_open_tasks`' shape exactly: a set with a discarding
        # done-callback, so a task neither leaks nor is collected mid-flight.
        self._auto_start_tasks: set = set()
        self._work_recorded: dict = {}
        # Writing a new card's instructions from its description
        # (card_prepare.py). Always offered; `prepare_card_text` is the
        # press, and the snapshot publishes `prepare_enabled` as True for
        # the phone.
        self._prepare_lock = asyncio.Lock()
        self._preparing = False
        self._last_prepare_at = 0.0

        # --- The Kanban board (board.py) and the launcher (dispatch.py) ---
        # Opened in run() like `_history`, and for the same reason: constructing
        # a BobDaemon must not create a database as a side effect.
        self._board: Optional[board.BoardStore] = None
        # The last computed board payload, held so `ApiServer.state()` — called
        # once per SSE frame — never touches SQLite on the event loop. Recomputed
        # on a mutation or a reconcile and at no other time.
        self._board_state: dict = {}
        # The manual-check badge's last published answer, per flagged card.
        # Memory only, like `_board_missing_since`: it is a drift detector for
        # a bit that is *derived* from the agents snapshot and never stored, so
        # a restart simply recomputes it on the first push. Keyed on the card
        # id, valued on the decided `manual_check_due` bit — never on raw
        # session membership, or every unflagged card's churn would republish
        # the board on every agents push.
        self._manual_due_seen: dict = {}
        self._needs_you_seen: dict = {}
        # The cost-and-time line's last published drift keys, per card with
        # a run — `run_figures.drift_key`; the same detector, for the frame
        # that draws a moved cent, minute or percent.
        self._run_figures_seen: dict = {}
        # The run-health line's last composed reading, per card with a
        # session row on the pass — `_run_health_drifted`, the same
        # detector: the reading is derived from the agents snapshot and
        # never stored, and the quantisation inside `run_health.compose` is
        # what bounds this to one frame per quantum rather than per turn.
        self._run_health_seen: dict = {}
        # Whether Dark Army may start a session at all. Plain attribute like the chime
        # and auto-compact above: the menu-bar toggle writes it from the AppKit
        # thread and `dispatch_card` reads it at the moment of the press.
        self.board_dispatch_enabled: bool = True
        # Whether Dark Army starts the head of a project's queue by itself when the
        # files it declares come free. Same plain-attribute shape as the flag
        # above, written by the AppKit thread and read at the moment the drain
        # fires. Default **on**: nothing is ever in the queue without a
        # deliberate human enqueue, so the automation only ever finishes
        # gestures already made — defaulted off, the queue becomes a parking
        # lot where cards a person explicitly started silently never start,
        # which is the board asserting an intent nobody is serving. Off
        # removes the drain entirely while the *gate* keeps working: cards
        # still queue, order is still kept, and a press still starts one.
        self.board_autostart_enabled: bool = True
        # How many agents may work at once in one project — the whole waiting
        # rule. Written by the AppKit thread through `set_board_parallel`,
        # which clamps, and read on the executor at the gate and at the drain.
        # A plain int rather than a preference read: the snapshot publishes
        # this attribute, so the number on screen is by construction the
        # number the gate obeys, even where `preferences.json` says 50.
        self.board_parallel_limit: int = BOARD_PARALLEL_MIN
        # And which projects say otherwise. `{canonical root: clamped int}`,
        # the `board_parallel_by_root` preference, resolved for every gate,
        # drain and readout by `_parallel_limit_for` — the machine dial above
        # is what a root without an entry gets. Written from the panel reader
        # thread by **replacing** the dict rather than mutating it, so the
        # executor never reads one mid-write.
        self.board_parallel_overrides: dict = {}
        # Card isolation, per project (`docs/card-worktrees.md`). The map
        # holds only the projects that switched it **off** — `{canonical
        # root: False}`, the `board_isolation_by_root` preference — read by
        # `_isolation_for` on the executor and replaced, never mutated, by
        # the panel reader thread, `board_parallel_overrides`' rule.
        self.board_isolation_overrides: dict = {}
        # Head card id → `{"project", "root", "since"}` while Dark Army is
        # preparing that card's worktree (the fetch, the add, the setup
        # script). Created and popped on the loop, **replaced, never
        # mutated**, so `_launch_inflight` and the decoration on the
        # executor may read it; the prepare task's `finally` always pops.
        self._worktree_preparing: dict = {}
        # `(card id, worktree path)` pairs the reconcile has already queued
        # once for a release look in this process — a Done card still naming
        # a folder after a restart, or one removed by hand. Memory only and
        # replaced, never mutated; a new run of the card drops its pair. The
        # "kept" line a card draws is derived from the card and the disk
        # (`_worktree_note`), never remembered here.
        self._worktree_considered: set = set()
        # Which model each agent and helper runs on: the machine-wide table
        # (`agent_models`, `{provider: {slot: model}}`) and the per-project
        # override map (`agent_models_by_root`, keyed on the canonical root),
        # resolved by `_agent_model_for` at the four launch sites. Both are
        # fed by the menu-bar app from `preferences.json` and, like the
        # parallel overrides above, **replaced, never mutated**: written from
        # the AppKit thread, read on the loop.
        self.agent_models: dict = {}
        self.agent_model_overrides: dict = {}
        # Cards the reconcile decided may start now, one per project, drained
        # on the loop by `_flush_queue_dispatches`. Decided on the executor
        # and acted on the loop — `_flush_auto_compacts`' pattern, and for its
        # reason: `dispatch_card` awaits a subprocess and must not run inside
        # the snapshot push.
        self._queue_candidates: list = []
        # Whether a card *arriving* in Done *disposes* the session's terminal
        # tab. A plain attribute the AppKit thread writes and
        # `_wrap_up_for_done` reads at the moment of the move. **Default
        # False** — disposing a tab is not undoable and an upgrade must
        # change nothing until asked; the menu-bar app overwrites this from
        # the `board_close_terminal` preference on startup either way.
        self.board_close_terminal_enabled: bool = False
        # Whether a started card runs on a terminal Dark Army itself owns
        # (`ptyhost`) rather than in the project's VS Code window. Same
        # plain-attribute shape, written by the AppKit thread from the
        # `board_own_terminal` preference and read by `_dispatch_card_locked`
        # at the moment of the press. Default **off**: a different place for
        # a session to live is not something an upgrade may choose.
        self.board_own_terminal_enabled: bool = False
        # card_id -> when Start was last accepted for it, for DISPATCH_COOLDOWN.
        self._dispatch_attempts: dict[str, float] = {}
        # card_id -> the session ids that already existed when Start was pressed.
        # Binding is by elimination, so the baseline is the whole mechanism: a
        # card takes the first session that was *not* in this set.
        self._dispatch_baseline: dict[str, set] = {}
        # card_id -> the shell pid of the terminal the extension opened for it —
        # the receipt that lets a bind *prove* a candidate session came out of
        # Dark Army's own terminal rather than merely appearing beside it. Memory-only
        # like `_dispatch_baseline`, and for the same reason: a daemon restart
        # must fall back to the predicate-only match rather than act on a
        # remembered pid that may belong to somebody else by now. Absent
        # entries (an extension older than 0.1.10, `processId` unresolved) mean
        # the no-receipt fallback, never a refusal to bind.
        self._spawn_shell_pids: dict[str, int] = {}
        # card_id -> the child pid of the terminal Dark Army itself opened for it
        # (`dispatch.spawn_local`). A separate map from `_spawn_shell_pids`
        # on purpose: that one is a *descent* proof against a terminal shell
        # the extension reported, this one is an *identity* — Dark Army is the
        # parent and knows the pid exactly. Memory-only for the same reason.
        self._spawn_pty_pids: dict[str, int] = {}
        # card_ids whose Start asked for Dark Army's own terminal while the
        # machine-wide preference was off. Memory-only: a queued replay
        # honours the press that enqueued it; a restart-after-enqueue falls
        # back to the preference rather than acting on a remembered pid.
        # Discarded by an ordinary Start, unqueue, reset or delete.
        self._own_terminal_dispatch: set[str] = set()
        # launch id -> an assistant started with **no card behind it**
        # (`open_adhoc_terminal`): `{"project", "root", "handle", "at"}`. It
        # exists for one job — occupying the one-per-project launch bound
        # while its session is still binding, so a card start and an ad-hoc
        # start can never race for "the first new session in this project".
        # Read *and swept* by `_launch_inflight`, which is the only reader:
        # a leaked entry would park every card start in that project for
        # good, so sweeping where it is read means it cannot outlive one
        # read. Memory-only, `_dispatch_baseline`'s argument exactly — a
        # restart must re-observe rather than inherit a countdown against a
        # pty handle it can no longer verify.
        self._adhoc_launches: dict[str, dict] = {}
        # When an ad-hoc terminal was last accepted, for DISPATCH_COOLDOWN.
        # One scalar rather than `_dispatch_attempts`' map: an ad-hoc launch
        # has no card id to key on, and the cooldown here is about the person
        # pressing the button twice.
        self._adhoc_attempt: float = 0.0
        # Mission Control's record (mission.py): the broker handle of the
        # one standing chief-of-staff terminal, its root and the last
        # session id bound to it. Loaded once here and written by
        # `_save_mission` on the executor; the terminal itself is the
        # broker's across a restart, this is only how to find it again.
        self._mission: dict = mission.load_record(paths_mod.MISSION_PATH)
        # When Mission Control was last accepted, for DISPATCH_COOLDOWN —
        # `_adhoc_attempt`'s shape, for the same reason.
        self._mission_attempt: float = 0.0
        # The terminals Dark Army owns. One host per daemon, installed as the
        # module-level answer `session_io` asks (`ptyhost.owns`). It hands
        # each child this daemon's hook door, so the child reports here.
        # Persist outside pytest: the broker process holds the master fds
        # so a Dark Army restart reconnects instead of SIGHUPing the agents.
        # The private hook socket is handed over in `run()`, once the door
        # has actually bound it (`_hand_terminals_the_hook_door`): until then,
        # and for good where the bind failed, a child is told the port.
        from .paths import PTY_PID_PATH, PTY_SOCK_PATH
        self._pty = ptyhost.PtyHost(
            port=HOOK_IPC_PORT,
            persist="pytest" not in sys.modules,
            sock_path=str(PTY_SOCK_PATH),
            pid_path=str(PTY_PID_PATH))
        ptyhost.install(self._pty)
        # The rest of a bounded away paint, one entry per hosted terminal
        # per viewer: `(handle, viewer)` → `(anchor_cursor, stamped_at,
        # remaining_bytes)`. Every paired phone polls the same handle, and
        # one drain must never eat another's slices. Read and written on
        # the loop alone, inside `terminal_frame` — its one reader and
        # writer — and pruned there; memory only.
        self._terminal_paint_tail: dict[tuple[str, str],
                                        tuple[int, float, bytes]] = {}
        # A terminal Dark Army owns is never titled: the OSC would land in Dark Army's
        # own stream, and the pane draws the name itself.
        self._titles.owned = self._pty.owns
        # The refinement's own copies of the two maps above. Separate rather
        # than shared because a refinement never touches `session_id`/
        # `link_state` — `bind_session` moves a card to In progress, and a
        # refining card must stay in Prep — so its baseline and its absence
        # timer cannot ride the dispatch's keys without the two verbs
        # clobbering each other on a card that is refined and later started.
        self._refine_baseline: dict[str, set] = {}
        self._refine_missing_since: dict[str, float] = {}
        # Pending helper sessions answering a card question. Memory only: a
        # daemon restart forgets them (same class as `_finished` tombstones).
        # Never persisted — a restored claim on "the next new session" is the
        # misbinding this ledger exists to avoid. Keyed by card id; at most
        # one pending consult per card.
        self._consults: dict[str, dict] = {}
        self._consult_attempts: dict[str, float] = {}
        # card_id -> when its session first stopped appearing in the fleet. In
        # memory rather than in the database, because it is a *timer*, not a
        # fact about the card: a daemon restart should re-observe the absence
        # rather than inherit a countdown it cannot verify.
        self._board_missing_since: dict[str, float] = {}
        # Serialises `dispatch_card` end to end. The guard reads the board, the
        # roots and the in-flight list and *then* awaits three more times before
        # it writes `dispatching` — so without this, two presses of the same
        # card (or N presses of N cards) both pass a guard computed on a state
        # neither of them had claimed yet, and both open a terminal. The bound
        # this file advertises has to be enforced, not merely computed. Held
        # across the spawn on purpose: a second press waiting a few seconds is
        # the cost of the bound being real.
        self._dispatch_lock = asyncio.Lock()
        # Serialises the *read-then-write* of a board mutation, and nothing
        # more. `update_card` and `reorder_card` both have to know which column
        # a card was in immediately before the write in order to tell an
        # arrival in Done from an edit of a card already there — and two
        # overlapping moves of the same card would otherwise both read a
        # non-`done` `before` and both fire a `/clear`, which is
        # `_dispatch_lock`'s lesson in a cheaper place. It is deliberately
        # *released* before the wrap-up: holding it across a VS Code round-trip
        # would serialise every board edit behind one terminal that is not
        # answering.
        self._board_write_lock = asyncio.Lock()

    # --- Observers ---
    #
    # There is more than one surface listening now: the menu bar (in-process) and
    # the panel API. They are peers — neither is privileged, and neither
    # may take the other down, so every dispatch is individually guarded.

    @property
    def _observer(self) -> Optional["DaemonObserver"]:
        """The first observer, or None. Kept because callers and tests set and read
        a single observer; new code should use add_observer/remove_observer."""
        return self._observers[0] if self._observers else None

    @_observer.setter
    def _observer(self, observer: Optional["DaemonObserver"]) -> None:
        self._observers = [observer] if observer is not None else []

    def add_observer(self, observer: "DaemonObserver") -> None:
        if observer is not None and observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(self, observer: "DaemonObserver") -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    def _notify_observers(self, method: str, *args) -> None:
        """Call `method` on every observer that implements it, guarded twice over.

        These dispatches run inside the staleness/liveness loops, so an observer
        that raises (one left on an older signature, say) would otherwise kill the
        task and stop eviction for the life of the daemon. And with two observers,
        one failing must not silence the other — hence per-observer isolation
        rather than one try around the loop."""
        for obs in list(self._observers):
            cb = getattr(obs, method, None)
            if cb is None:
                continue
            try:
                cb(*args)
            except Exception:
                logger.warning("%s observer failed", method, exc_info=True)

    def _observers_implementing(self, method: str) -> list:
        return [o for o in self._observers if getattr(o, method, None) is not None]

    #: Every folder Dark Army has turned away, newest activity last. Memory only, and
    #: bounded at `MAX_UNENROLLED`. Each entry is
    #: ``{"root", "label", "last_seen", "count"}``; `other` counts the refusals
    #: whose claimed path was not corroborated (see `_note_unenrolled`).
    _unenrolled: dict
    _unenrolled_other: int

    def _enrolled_root(self, msg: dict) -> str:
        """The enrolled project this message's key names, or "".

        One predicate, `_plan_gate_refusal`'s shape, applied above every branch
        of `_handle_message` so it covers the channel messages, the five board
        verbs, the statusline reply and the whole hook stream at once. There is
        deliberately no "a message with no key is allowed" grace: that is the
        same hole as an empty password comparing equal to an absent field.
        """
        return enrollment.resolve(str(msg.get("key") or ""))

    def _note_unenrolled(self, msg: dict) -> None:
        """Remember a folder Dark Army just turned away, so the panel can offer it.

        **The claimed `cwd` is untrusted** — it arrived on a socket that
        authenticates nothing — so a path is only kept when it resolves inside
        the folder of an open VS Code window, which is a local read nothing on
        that socket controls. Everything else increments a bare integer. Without
        that, a forged message could put an arbitrary path in front of somebody
        and the Enrol button beside it would write a key into that path.
        """
        claimed = str(msg.get("cwd") or "")
        if not claimed and isinstance(msg.get("data"), dict):
            workspace_block = msg["data"].get("workspace") or {}
            if isinstance(workspace_block, dict):
                claimed = str(workspace_block.get("current_dir") or "")
        root = ""
        if claimed:
            try:
                here = dispatch.normalise_root(claimed)
            except (OSError, ValueError):
                here = ""
            if here:
                for window in workspace.windows():
                    for folder in window.folders:
                        folder = dispatch.normalise_root(folder)
                        if folder and (here == folder
                                       or here.startswith(folder + os.sep)):
                            if len(folder) > len(root):
                                root = folder
        if not root:
            self._unenrolled_other = min(self._unenrolled_other + 1, 10 ** 6)
            return
        # A refusal from a folder that *is* enrolled is a stale client, not an
        # unenrolled project — a channel process still running the copy it was
        # launched with, which sends no key and is refused exactly like a wrong
        # one. Naming it here would be false on its face (the project's own rows
        # are on screen beside the banner saying Dark Army is ignoring it) and the
        # Enrol button next to it would do nothing. Counted as `other` instead.
        if enrollment.root_enrolled(root):
            self._unenrolled_other = min(self._unenrolled_other + 1, 10 ** 6)
            return
        entry = self._unenrolled.get(root) or {
            "root": root, "label": os.path.basename(root) or root, "count": 0,
        }
        entry["last_seen"] = time.time()
        entry["count"] = int(entry.get("count") or 0) + 1
        self._unenrolled[root] = entry
        if len(self._unenrolled) > MAX_UNENROLLED:
            oldest = min(self._unenrolled.values(),
                         key=lambda e: e.get("last_seen") or 0.0)
            self._unenrolled.pop(oldest["root"], None)

    def enrollment_snapshot(self) -> dict:
        """What the panel is shown about enrolment. One function, called from
        both `detailed_snapshot()` and the API's `state()`, so the two surfaces
        cannot disagree. **No key and no digest ever appears here.**"""
        out = enrollment.snapshot()
        # Filtered again at publication, not only at insertion: enrolling a
        # folder must clear its banner on the *next frame*, without waiting for
        # a memory-only entry recorded seconds earlier to age out of a list
        # nothing prunes on enrolment.
        pending = sorted(
            (e for e in self._unenrolled.values()
             if not enrollment.root_enrolled(e.get("root") or "")),
            key=lambda e: e.get("last_seen") or 0.0, reverse=True)
        out["pending"] = [{"root": e["root"], "label": e["label"],
                           "last_seen": e.get("last_seen", 0.0)}
                          for e in pending]
        out["other"] = self._unenrolled_other
        # The first-run checklist's evidence, read off the last snapshot
        # cycle and never computed here. `available` is stated: an older
        # daemon publishes no `checklist` at all, and the panel keeps its
        # ordinary enrolment UI on that absence rather than drawing steps
        # it has no facts for. Roots that have left the ledger since the
        # last cycle are filtered at publication, `pending`'s discipline.
        enrolled_now = {row["root"] for row in out.get("enrolled", [])}
        out["checklist"] = {
            "available": True,
            "roots": [dict(fact) for fact in getattr(self, "_checklist_facts", ())
                      if fact.get("root") in enrolled_now],
        }
        return out

    def _observe_checklist(self, snapshot: dict) -> tuple:
        """Gather the checklist's facts from the snapshot just built.

        Executor-only, on the agents-push path: `workspace.windows()` is the
        same cached read project naming already makes, and the ledger read is
        memoised on its stat. Main agents only — the rows `_bindable_candidate`
        admits from the three live buckets; finished and abandoned rows are
        history, not a session somebody just started. Dark Army's own
        checkout is marked from `enrollment.self_root()`, memoised per
        process, so this cycle walks no ancestors and reads no stamp.
        """
        rows = []
        for bucket in ("running", "sleeping", "waiting"):
            for row in (snapshot or {}).get(bucket, []) or []:
                if isinstance(row, dict) and self._bindable_candidate(row):
                    rows.append(row)
        return checklist_facts(
            enrollment.enrolled_roots(), workspace.windows(), rows,
            own_root=enrollment.self_root())

    def inbox_snapshot(self) -> dict:
        """Published Needs you acknowledgements. `available` is stated."""
        store = getattr(self, "_inbox_acks", None)
        if store is None:
            return {"available": True, "acks": []}
        # Missing cache is not an empty fleet. The mixin default is a
        # class-level `{}`; only an instance assignment (the first agents
        # push) counts. Pruning before that would wipe every session ack
        # on restart, because SSE attach calls state() immediately.
        if "_agents_snapshot_cache" in self.__dict__:
            store.prune(self._inbox_live_keys())
        return {"available": True, "acks": store.records()}

    def bearings_snapshot(self, since=None) -> dict:
        """The four-section Bearings digest, loop-side and I/O-free.

        Reads the published agents snapshot, prompts, notification cards,
        board cards, inbox acks, the diary's in-memory recent rows and
        Mission Control's session id. A missing agents cache is an empty
        fleet, not an error; a missing diary is `diary_available: false`
        with Recently landed empty. Mission Control's own row is skipped.
        """
        if "_agents_snapshot_cache" in self.__dict__:
            snapshot = self._agents_snapshot_cache or {}
        else:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        prompts = self._prompts_by_session()
        notified = [n["session_id"] for n in self._notification_snapshot()]
        cards = (self._board_state or {}).get("cards")
        acks = self.inbox_snapshot()["acks"]
        diary = getattr(self, "_event_log", None)
        if diary is None:
            events = []
            diary_available = False
        else:
            events = diary.recent(since=since, limit=bearings.MAX_EVENTS)
            diary_available = True
        mission_sid = str((self.mission_snapshot() or {}).get("session_id") or "")
        skip = (mission_sid,) if mission_sid else ()
        return bearings.compose(
            snapshot=snapshot,
            prompts=prompts,
            notified=notified,
            cards=cards,
            acks=acks,
            events=events,
            diary_available=diary_available,
            since=since,
            now=time.time(),
            skip_sessions=skip,
        )

    def _inbox_live_keys(self) -> set[str]:
        keys: set[str] = set()
        snapshot = getattr(self, "_agents_snapshot_cache", None)
        if isinstance(snapshot, dict):
            for rows in snapshot.values():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sid = str(row.get("session_id") or "")
                    if sid:
                        keys.add("s:" + sid)
        # A first assigned-but-empty cache must not empty the file: hook
        # state and tombstones still name sessions the hide is about.
        for sid in getattr(self, "_session_states", None) or {}:
            if sid:
                keys.add("s:" + sid)
        for sid in getattr(self, "_finished", None) or {}:
            if sid:
                keys.add("s:" + sid)
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = str(card.get("id") or "")
            if cid:
                keys.add("c:" + cid)
        return keys

    def _inbox_session_entry(self, sid: str):
        """`(entry, in_waiting)` for a session id, or `(None, False)`."""
        snapshot = getattr(self, "_agents_snapshot_cache", None) or {}
        found = None
        in_waiting = False
        if not isinstance(snapshot, dict) or not sid:
            return None, False
        waiting = snapshot.get("waiting") or []
        if isinstance(waiting, list):
            in_waiting = any(
                isinstance(row, dict) and str(row.get("session_id") or "") == sid
                for row in waiting)
        for rows in snapshot.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and str(row.get("session_id") or "") == sid:
                    found = row
                    break
            if found is not None:
                break
        return found, in_waiting

    def _inbox_session_questions(self, sid: str, entry: Optional[dict]) -> list:
        if isinstance(entry, dict):
            questions = list(entry.get("questions") or [])
            question = entry.get("question") or {}
            if not questions and isinstance(question, dict) and question.get("text"):
                questions = [question]
            if questions:
                return questions
        pending = (getattr(self, "_questions", None) or {}).get(sid) or {}
        if not pending:
            pending = (getattr(self, "_pending_questions", None) or {}).get(sid) or {}
        if not isinstance(pending, dict):
            return []
        questions = list(pending.get("questions") or [])
        if not questions and pending.get("text"):
            questions = [pending]
        return questions

    def _inbox_session_has_permission(self, sid: str) -> bool:
        snap = getattr(self, "_permission_snapshot", None)
        if snap is None or not sid:
            return False
        rows = snap()
        return any(str(row.get("session_id") or "") == sid
                   for row in (rows or []) if isinstance(row, dict))

    def _inbox_live(self, key: str):
        """`(kind, fp)` for a live inbox subject, else None."""
        if key.startswith("s:"):
            sid = key[2:]
            entry, in_waiting = self._inbox_session_entry(sid)
            has_note = sid in (getattr(self, "_active_notifications", None) or {})
            has_permission = self._inbox_session_has_permission(sid)
            questions = self._inbox_session_questions(sid, entry)
            waiting = in_waiting or has_note
            if entry is None and not has_permission and not questions and not waiting:
                return None
            return inbox_ack.session_kind_and_fp(
                permission=has_permission, questions=questions, waiting=waiting)
        if key.startswith("c:"):
            cid = key[2:]
            cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
            for card in cards:
                if isinstance(card, dict) and str(card.get("id") or "") == cid:
                    return inbox_ack.card_kind_and_fp(card)
            return None
        return None

    async def ack_inbox(self, key, kind, fingerprint) -> tuple[bool, str]:
        """Hide one Needs you subject until it changes.

        For a card subject that is the whole of it. For a session subject
        the hide list is joined by `_settle_acknowledged_session`, so the
        row goes quiet everywhere and not only on the Inbox."""
        key = key if isinstance(key, str) else ""
        kind = kind if isinstance(kind, str) else ""
        fingerprint = fingerprint if isinstance(fingerprint, str) else ""
        if not inbox_ack.valid_key(key):
            return False, inbox_ack.INBOX_ACK_MISSING_REFUSAL
        if kind in inbox_ack.NEVER_KINDS or kind not in inbox_ack.ACK_KINDS:
            return False, inbox_ack.INBOX_ACK_KIND_REFUSAL
        live = self._inbox_live(key)
        if live is None:
            return False, inbox_ack.INBOX_ACK_MISSING_REFUSAL
        live_kind, live_fp = live
        if live_kind != kind or live_fp != fingerprint:
            return False, inbox_ack.INBOX_ACK_STALE_REFUSAL
        if live_kind not in inbox_ack.ACK_KINDS:
            return False, inbox_ack.INBOX_ACK_KIND_REFUSAL
        store = getattr(self, "_inbox_acks", None)
        if store is None:
            return False, inbox_ack.INBOX_ACK_MISSING_REFUSAL
        store.ack(key, kind, fingerprint)
        if key.startswith("s:"):
            await self._settle_acknowledged_session(key[2:], kind)
        if kind == "start_asked" and key.startswith("c:"):
            # Dismiss on an asked start is the person's No: the ask goes,
            # rather than lingering hidden until it times out.
            await self.drop_start_ask(key[2:])
        await self._wake_surfaces()
        return True, ""

    # -- the phone doors' access log --------------------------------------------

    async def note_access_refusal(self, door: str, peer: str, reason: str,
                                  device_id: str = "") -> None:
        """One refused knock on a phone door, written down and weighed.

        Called on the loop by `ApiServer._record_access` (fire-and-forget)
        after the door has already answered — the verdict never depends on
        this. The journal append goes to the executor (`_log_event`'s
        shape); the detector is pure and runs here, under
        `_access_alert_lock` together with the raise or fold it decides,
        so concurrent knocks fold into the alert the first one raised. A
        burst raises the alert through `_raise_access_alert`.
        """
        log = getattr(self, "_access_log", None)
        if log is None:
            return
        door = str(door or "")
        peer = access_log.clamp_peer(peer)
        reason = str(reason or "")
        device_id = access_log.clamp_peer(device_id)
        loop = asyncio.get_running_loop()
        # Awaited, so the fifth refusal is on the file before the burst line
        # its alert writes — the log reads in the order things happened.
        await loop.run_in_executor(None, functools.partial(
            log.append, "refusal", door=door, peer=peer, reason=reason,
            device_id=device_id))
        detector = getattr(self, "_burst_detector", None)
        if detector is None:
            return
        # The decision and its journal write are one critical section:
        # `_raise_access_alert` sets `_access_fold` only after its own
        # executor hop, and a concurrent knock's fold must land after it.
        lock = getattr(self, "_access_alert_lock", None)
        if lock is None:
            lock = self._access_alert_lock = asyncio.Lock()
        async with lock:
            alert = detector.note(peer, reason, time.time(), device_id=device_id)
            if alert and alert.get("fold"):
                await self._fold_access_alert(door, alert)
            elif alert:
                await self._raise_access_alert(door, alert)

    async def note_link_timing(self, door: str, device_id: str, kind: str,
                               hops: dict, status: int, size: int) -> None:
        """One timed request on a phone door, added to the link timer.

        Called on the loop by `ApiServer.note_link_timing` (fire-and-forget)
        after the door has answered — the answer never depends on this.
        `LinkTimer.add` is pure and O(1); only a closed ten-minute window
        costs anything, and that one `timing` append goes to the executor
        (`_log_event`'s hop). Nothing here weighs a request, nothing here
        raises an alert, and the detector is never consulted.
        """
        timer = getattr(self, "_link_timer", None)
        if timer is None:
            return
        door = str(door or "")
        device_id = access_log.clamp_peer(device_id)
        rollup = timer.add(door, device_id, str(kind or ""), hops, status,
                           size, time.time())
        if rollup is None:
            return
        log = getattr(self, "_access_log", None)
        if log is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, functools.partial(
            log.append, "timing", door=rollup["door"], peer=device_id,
            device_id=device_id, count=int(rollup["count"]),
            window_seconds=int(rollup["window_seconds"]),
            hops=dict(rollup["hops"])))

    async def _fold_access_alert(self, door: str, alert: dict) -> None:
        """A burst inside `ALERT_FLOOR_SECONDS` of the last raise: one
        `burst_more` line on the access log carrying the running totals,
        and nothing else — no diary line, no alert row, no banner, no wake.
        The updated count rides the next ordinary frame (a wake per fold
        would be a per-knock cost a LAN attacker controls). With no open
        alert to fold into the burst is dropped, never promoted to a raise:
        the detector said the floor is up."""
        log = getattr(self, "_access_log", None)
        fold = getattr(self, "_access_fold", None)
        if log is None or fold is None:
            logger.debug("access burst inside the floor with no open alert; dropped")
            return
        peer = access_log.clamp_peer(alert.get("peer"))
        fold["count"] = int(fold.get("count") or 0) + int(alert.get("count") or 0)
        peers = fold.setdefault("peers", set())
        if peer not in peers and len(peers) < access_log.MAX_FOLDED_PEERS:
            peers.add(peer)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, functools.partial(
            log.append, "burst_more", door=str(door or ""), peer=peer,
            alert_id=str(fold.get("alert_id") or ""), count=fold["count"],
            peers=len(peers),
            window_seconds=int(alert.get("window_seconds") or 0)))

    async def _raise_access_alert(self, door: str, alert: dict) -> None:
        """A burst became an alert: one `burst` line on the access log, one
        `access_burst` line on the diary, one row delivered at once through
        `_deliver_batch` — the Mac banner and the phone buzz, the queued
        agent alerts left for the snapshot's own drain — and a wake so the
        `security` section rides the next frame."""
        log = getattr(self, "_access_log", None)
        if log is None:
            return
        alert_id = secrets.token_hex(6)
        now = time.time()
        peer = access_log.clamp_peer(alert.get("peer"))
        count = int(alert.get("count") or 0)
        window = int(alert.get("window_seconds") or 0)
        loop = asyncio.get_running_loop()
        # Awaited before the banner goes out: `security_snapshot` reads
        # `open_alerts` off the journal, so a frame woken by the delivery
        # below must already find the burst line — never a banner beside an
        # empty `security.alerts`.
        await loop.run_in_executor(None, functools.partial(
            log.append, "burst", id=alert_id, ts=now, door=door, peer=peer,
            count=count, window_seconds=window))
        self._access_fold = {"alert_id": alert_id, "door": door, "peer": peer,
                             "count": count, "peers": {peer}}
        self._log_event("access_burst", detail={
            "door": access_log.door_label(door), "peer": peer,
            "count": count, "window_seconds": window})
        text = access_log.sentence("burst", door=door, peer=peer, count=count,
                                   window_seconds=window)
        row = {
            "id": "access:" + alert_id,
            "session_id": "",
            "nickname": "",
            "title": "Dark Army refused repeated connection attempts",
            "subtitle": access_log.door_word(door),
            "body": text,
            "severity": "crit",
            "rule": "access_burst",
            "created_at": now,
            "actions": [],
            "character": "",
            "state": "",
            "kind": alerting.KIND_SECURITY,
        }
        self._alerts = (self._alerts + [row])[-32:]
        # Its own row only, and the queue is never rebound: agent alerts
        # already queued were decided on a snapshot not yet published, and
        # `_push_agents_snapshot` drains them after publishing — drained
        # here, the phone leg would judge them against the stale list.
        self._deliver_batch([row])
        await self._wake_surfaces()

    def security_snapshot(self) -> dict:
        """The open burst alerts, judged from the access log — evidence
        neither client has on the wire, so both consume it and re-derive
        nothing. `available` is stated: a daemon whose log failed to open
        says so rather than looking like a quiet machine.

        Capped at the newest `MAX_PUBLISHED_ALERTS`, oldest first, with
        `hidden` counting the rest; `ack_access_alert` checks the uncapped
        `open_alerts`, so a hidden alert still acks. The unavailable shape
        stays the two-key one an older client knows."""
        log = getattr(self, "_access_log", None)
        if log is None:
            return {"available": False, "alerts": []}
        rows = log.open_alerts(time.time())
        hidden = max(0, len(rows) - access_log.MAX_PUBLISHED_ALERTS)
        alerts = []
        for row in rows[-access_log.MAX_PUBLISHED_ALERTS:]:
            alerts.append({
                "id": str(row.get("id") or ""),
                "ts": float(row.get("ts") or 0.0),
                "door": str(row.get("door") or ""),
                # The door in a person's word ("Wi-Fi", "relay", "pairing",
                # "upload"), composed here so neither client draws the
                # token; an older client ignores the key.
                "door_word": access_log.door_label(str(row.get("door") or "")),
                "peer": str(row.get("peer") or ""),
                "count": int(row.get("count") or 0),
                "window_seconds": int(row.get("window_seconds") or 0),
                "text": str(row.get("text") or ""),
                # The fold totals `open_alerts` merged: distinct sources so
                # far and the newest fold's clock (`last_seen` is a
                # `_CLOCK_FIELDS` name on purpose — a fold that only moves
                # the clock is not news; a changed `count` is).
                "peers": int(row.get("peers") or 1),
                "last_seen": float(row.get("last_seen") or row.get("ts") or 0.0),
            })
        return {"available": True, "alerts": alerts, "hidden": hidden}

    async def ack_access_alert(self, alert_id) -> tuple[bool, str]:
        """Close one burst alert on both surfaces. Clear-never-set: the
        only thing this can do is take an open alert off the list, and the
        log entry stays."""
        if not isinstance(alert_id, str) or not alert_id.strip():
            return False, access_log.ACCESS_ALERT_MISSING_REFUSAL
        log = getattr(self, "_access_log", None)
        if log is None:
            return False, access_log.ACCESS_ALERT_MISSING_REFUSAL
        alert_id = alert_id.strip()
        open_ids = {str(r.get("id") or "") for r in log.open_alerts(time.time())}
        if alert_id not in open_ids:
            return False, access_log.ACCESS_ALERT_MISSING_REFUSAL
        peer = ""
        for row in log.open_alerts(time.time()):
            if str(row.get("id") or "") == alert_id:
                peer = str(row.get("peer") or "")
                break
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, functools.partial(
            log.append, "burst_ack", alert_id=alert_id, peer=peer))
        await self._wake_surfaces()
        return True, ""

    async def _settle_acknowledged_session(self, sid: str, kind: str) -> None:
        """What acknowledging a *session* subject does beyond the hide list.

        The hide list is the Inbox's business alone: the notification card
        and the row's error state still bank the session under `waiting`,
        so the strip, the widget, the Agents row and the tab dot kept saying
        "needs you" after a person had said they had seen it. A session
        acknowledgement therefore also does what Clear all does per row —
        `dismiss_notification` — and, for a plain wait, demotes `confused` /
        `error` to `idle` the way `low_priority_session` does, so the row
        reads `sleeping` until its next hook event. A `waiting` *state* is
        left alone: that is an AskUserQuestion dialog still up, and a
        question stays on the row regardless (the hide list is what takes a
        Codex question off the inbox). A card subject changes nothing here —
        a card's flag is a board fact only a board verb clears.
        """
        changed = False
        if kind == "waiting":
            st = self._session_states.get(sid)
            if st is not None and st.get("state") in ("confused", "error"):
                st["state"] = "idle"
                st["last_event"] = time.time()
                st["last_event_monotonic"] = time.monotonic()
                changed = True
        if sid in self._active_notifications:
            await self.dismiss_notification(sid)
        elif changed:
            self._persist_sessions()

    async def _handle_message(self, msg: dict) -> Optional[dict]:
        """Handle a message from dark-army-notify via the socket.

        Returns None for the hook events (fire-and-forget), or a reply dict for
        the statusline collector, which the socket server writes back."""
        # The door. Above every branch below, so one predicate covers the
        # channel messages, the five board verbs, the statusline reply and the
        # hook stream — and checked on *every* message rather than once per
        # session, so un-enrolling a project bites on the next thing it says
        # rather than at the end of a run that may last hours.
        if not self._enrolled_root(msg):
            self._note_unenrolled(msg)
            sid = str(msg.get("session_id") or "")
            if sid and sid in self._session_states:
                # A project un-enrolled mid-run must leave no half-present row —
                # but *this session's own folder* is what decides that, never
                # the arriving message. A refusal merely names an id, and both
                # `/api/state` (ungated) and the hook socket (which
                # authenticates nothing) hand ids out; a stale `channel_attach`
                # from a channel copy launched before enrolment shipped names
                # one every 30s, and forgetting on that made a live row flap.
                # `msg["cwd"]` and `msg["key"]` are deliberately not consulted
                # here, for the same reason `_note_unenrolled` corroborates a
                # claimed folder before it will even name it. An empty cwd is a
                # keep, matching `_collect_agent_stubs`' empty-cwd sweep.
                cwd, _ = self._session_place(sid)
                if cwd and not enrollment.root_enrolled(cwd):
                    self._forget_session(sid, "unenrolled")
            kind = msg.get("type")
            if kind in ("board_card_request", "board_close_request",
                        "board_attach_request", "board_attach_report_request",
                        "board_manual_request",
                        "board_answer_request",
                        "knowledge_read_request", "knowledge_write_request",
                        "board_start_ask_request", "board_next_request"):
                # These ride a `tools/call`. Refused *with a reply*: an
                # unanswered id is a session waiting for ever, which is worse
                # than the thing being prevented.
                return {"ok": False, "error": ENROLLMENT_REFUSAL}
            if msg.get("event") == "statusline":
                # An empty reply, not a refusal string: the collector prints
                # its own line either way, and `others_waiting` must not cross
                # into a project Dark Army was never told to watch.
                return {}
            return None
        # Three routers, one thin dispatcher. Nothing below the gate decides
        # anything; it only names which conversation this message belongs to.
        if (msg.get("type") in CHANNEL_MESSAGE_TYPES
                or msg.get("event") == "statusline"):
            return await self._route_channel(msg)
        if msg.get("event") in LIFECYCLE_EVENTS:
            return await self._route_lifecycle(msg)
        return await self._route_control(msg)

    async def _route_channel(self, msg: dict) -> Optional[dict]:
        """The messages that are *answered*: channel envelopes, the five board
        verbs riding a `tools/call`, and the statusline reply.

        Only reached below `_enrolled_root` in `_handle_message` — never call
        this on an unvetted message."""
        # Channels speak `type`, hooks speak `event`. Kept apart on purpose:
        # a hook describes something a session *did*, and these two describe a
        # session Dark Army can now talk *to*.
        if msg.get("type") in ("channel_attach", "channel_permission_request"):
            return self._handle_channel_message(msg)
        # The channel messages that need an *answer*: a `tools/call` id left
        # unanswered is a session waiting forever. Handled here rather than in
        # `_handle_channel_message` because they await (the board is SQLite on
        # the executor) and that method is synchronous and called directly by
        # tests. Both board verbs sit here for that one reason.
        if msg.get("type") == "board_card_request":
            return await self._handle_board_card_request(msg)
        if msg.get("type") == "board_close_request":
            return await self._handle_board_close_request(msg)
        if msg.get("type") == "board_attach_request":
            return await self._handle_board_attach_request(msg)
        if msg.get("type") == "board_attach_report_request":
            return await self._handle_board_attach_report_request(msg)
        if msg.get("type") == "board_manual_request":
            return await self._handle_board_manual_request(msg)
        if msg.get("type") == "board_answer_request":
            return await self._handle_board_answer_request(msg)
        if msg.get("type") == "knowledge_read_request":
            return await self._handle_knowledge_read_request(msg)
        if msg.get("type") == "knowledge_write_request":
            return await self._handle_knowledge_write_request(msg)
        if msg.get("type") == "board_start_ask_request":
            return await self._handle_board_start_ask_request(msg)
        if msg.get("type") == "board_next_request":
            return await self._handle_board_next_request(msg)
        if msg.get("event") == "statusline":
            return self._handle_statusline(msg)
        return None

    async def _route_control(self, msg: dict) -> Optional[dict]:
        """Anything wearing neither a channel `type` nor a lifecycle `event`.

        Deliberately the same tail `_route_lifecycle` runs: an unrecognised
        message always took the hook path — `_update_session_state` shrugs at
        an event it does not know, and the logging, pending-question tracking
        and surface wake still happen. Splitting the routers must not change
        what any message does; a control-only treatment would be a behaviour
        change, not a refactor."""
        return await self._route_lifecycle(msg)

    def _claim_project(self, session_id: str, project: str) -> bool:
        """Store the project a message names on the session Dark Army holds, and
        say whether that was news. Nothing happens for an empty claim or a
        session not held yet (a later call covers the one just created)."""
        entry = self._session_states.get(session_id) if session_id else None
        if not project or entry is None or entry.get("project") == project:
            return False
        entry["project"] = project
        return True

    def _log_hook_message(self, msg: dict, event, hook, session_id: str, project: str) -> None:
        """One log line per hook message: what it was, whose, and the few
        fields that tell one apart from the next."""
        details = []
        if event in _EVENTS_NAMING_A_TOOL:
            details.append(f"tool={msg.get('tool_name', '?')}")
        elif event in _EVENTS_NAMING_A_CHILD:
            details.append(f"agent={msg.get('agent_id', '?')[:12]}")
        elif event == "add":
            details.append(f"msg={msg.get('message', '')[:30]}")
        details.extend(f"{key}={msg[key]}" for key in ("source", "reason", "pid") if msg.get(key))
        held = self._session_states.get(session_id) if session_id else None
        named = ((held or {}).get("project") or project) if session_id else ""
        logger.info("Hook message: event=%s hook=%s session=%s%s%s",
                    event, hook, session_id[:12], f" [{named}]" if named else "",
                    "".join(" " + item for item in details))

    def _drop_card(self, session_id: str) -> None:
        self._active_notifications.pop(session_id, None)

    def _raise_card(self, session_id: str, msg: dict) -> None:
        # Stop and then Claude Code's own idle Notification both say "waiting
        # for input": the second refreshes the card already showing and does
        # not chime again. Only a card that is new arms the chime.
        fresh = session_id not in self._active_notifications
        self._active_notifications[session_id] = msg
        if fresh:
            self._pending_sound_sessions.add(session_id)
            self._schedule_notification_sound()

    def _drop_waiting_card(self, session_id: str, why: str) -> None:
        """Take down a Stop / idle-Notification card whose session is plainly
        busy again. A StopFailure card stays: it is an API error the person
        still wants to see, and the retry after it is itself a tool call."""
        card = self._active_notifications.get(session_id)
        if card and card.get("hook") in PARKED_CARD_HOOKS:
            self._drop_card(session_id)
            logger.info("Dismissing stale %s card for %s — %s",
                        card.get("hook"), session_id[:12], why)

    def _forget_cleared_predecessors(self, session_id: str, pid: int) -> None:
        """A SessionStart from a pid that another session, active within
        `PID_DEDUP_FRESHNESS_SECONDS`, already holds is that process's
        `/clear`: the earlier session has ended, and is forgotten as such."""
        now_mono = time.monotonic()
        earlier = [
            sid for sid, entry in self._session_states.items()
            if sid != session_id and entry.get("pid") == pid
            and now_mono - entry.get("last_event_monotonic", now_mono) < PID_DEDUP_FRESHNESS_SECONDS
        ]
        for sid in earlier:
            logger.info("Session %s ended by /clear: PID %d now runs session %s",
                        sid[:12], pid, session_id[:12])
            self._forget_session(sid, "cleared")

    async def _route_lifecycle(self, msg: dict) -> Optional[dict]:
        """The hook stream: a session's own life, fire-and-forget.

        Only reached below `_enrolled_root` in `_handle_message`."""
        event = msg.get("event")
        session_id = msg.get("session_id", "")
        hook = msg.get("hook", "")
        # The broker's poll is a lookup and nothing else: no state effect, no
        # logging of a session's life, no surface wake. Answered and done.
        if event == "permission_poll":
            return self._handle_permission_poll(msg)
        # Its ask, on the other hand, *is* a permission event — the same one
        # the legacy handler has always sent. So the hold is decided here and
        # the message then runs the whole `permission` path below unchanged:
        # the pending-question guard, the `waiting` state, the child redirect,
        # the logging. One event, one state effect, whether or not the hold
        # was granted.
        broker_reply: Optional[dict] = None
        if event == "permission_ask":
            broker_reply = self._decide_permission_ask(msg)
            msg = dict(msg)
            msg["event"] = "permission"
            event = "permission"
        # Grok's permission hook is not a prompt. Its `permission_prompt`
        # Notification fires for every tool call, because Grok runs a
        # permission phase on each one and resolves an allowed tool in
        # milliseconds (`wait_ms: 0` in its own `events.jsonl`); the hook
        # then landed here as `permission`, `_enter_state` put the row in
        # `waiting`, and nothing cleared it until the next tool_use — a
        # forty-second shell command showed "waiting for you" for its whole
        # run with no dialog anywhere. Asked of the session's own log
        # before the state moves: a request that already has its
        # resolution is a touch, not a wait. The hook's own id, before the
        # child→parent redirect below: a child's tools are in the child's
        # file. An unreadable log or a tail with no request keeps the hook's
        # reading, as does a genuinely open request — that is the prompt.
        if event == "permission" and msg.get("provider") == "grok" \
                and self._grok_permission_already_resolved(session_id, msg):
            msg = dict(msg)
            msg["event"] = event = "permission_resolved"
        elif event == "permission" and msg.get("provider") == "grok":
            self._register_grok_permission(session_id, msg)
        if event in ("subagent_start", "subagent_stop"):
            session_id = self._resolve_subagent_parent(session_id, msg)
        else:
            parent = self._parent_of_child(session_id)
            if parent:
                if event not in CHILD_WORK_EVENTS:
                    logger.info(
                        "Dropping %s from subagent %s of %s",
                        event or hook or "?", session_id[:12], parent[:12],
                    )
                    return None
                # A row we should never have opened. Dropped without a reason,
                # so it leaves no tombstone: a subagent is not a run that
                # finished, and "Recently finished" is not where it belongs.
                if session_id in self._session_states:
                    logger.info(
                        "Dropping stray subagent row %s — it belongs to %s",
                        session_id[:12], parent[:12],
                    )
                    self._forget_session(session_id)
                session_id = parent
                if event == "permission" and parent in self._session_states:
                    self._session_states[parent]["ask_note"] = (
                        "Raised inside a helper agent — answer it in the terminal.")
        if ((event == "dismiss" and hook == "UserPromptSubmit")
                or event == "tool_use"):
            state = self._session_states.get(session_id)
            if state is not None:
                state["ask_note"] = ""
        # Store project name on session if provided. Whether it *differed* is
        # remembered: this early write happens before `_update_session_state`
        # computes `changed`, so the later diff check would see its own value
        # and a message whose only news was the project would never persist.
        project = msg.get("project", "")
        project_changed = self._claim_project(session_id, project)

        # The transcript claim is stored on the same terms and for the same
        # reason it is stored *early*: the Stop that raises the card carries
        # the path itself, and `_async_subagent_reason` reads it a few lines
        # below. Left to the write further down — after the card decision —
        # the parking rung would consult an empty string on the very turn it
        # exists for, park nothing, and the false card would be raised anyway.
        # Not `changed`: a stored path is bookkeeping, not news.
        # Clamped like every other claimed string on this path: it is stored
        # in `sessions.json` and never published, but an unbounded payload
        # field has no business being written to disk verbatim.
        transcript_claim = str(msg.get("transcript_path") or "")[:1024]
        if transcript_claim and session_id and session_id in self._session_states:
            self._session_states[session_id]["transcript_path"] = \
                transcript_claim

        self._log_hook_message(msg, event, hook, session_id, project)

        # A Stop / idle_prompt Notification while the session is still running
        # background agents is not a "you're needed" prompt — the parent has
        # merely parked its turn to wait for them, not for the human. A later
        # Stop, once the background work is done, raises the card normally.
        # (AskUserQuestion/PermissionRequest are a separate 'permission'/'waiting'
        # path and never reach this card branch.)
        #
        # This used to suppress only the card — and the card is the smaller half.
        # `_update_session_state` went on to set the very same event's state
        # (`idle` for Stop, `confused` for Notification), and `categorize` reads
        # both of those as `waiting` whether or not a card exists. So a parked
        # agent still landed under "Needs you", still counted toward the strip's
        # attention figure, and still qualified for an alert; the suppression
        # bought silence and nothing else. Measured by replaying one session's
        # real event stream: 63 seconds pinned under "Needs you" with no card,
        # ending only because the next tool call happened to arrive. Hence
        # `parked` is threaded into the state update as well.
        parked = ""
        quiet = False

        if event == "add":
            # Enrich with the Claude session name (ai-title) so the card shows
            # WHICH session is asking. Cached, mtime-invalidated.
            msg["session_title"] = self._session_title(
                session_id, msg.get("transcript_path", ""))
            held = self._session_states.get(session_id)
            parked = self._parked_reason(held) if hook in PARKED_CARD_HOOKS else ""
            if not parked and hook in PARKED_CARD_HOOKS:
                # The live set's blind spot, and it is the commonest way this
                # machine raised a card nobody wanted: an async spawn is
                # struck from `subagents` seconds after it starts, so
                # `_parked_reason` — which is pure, and stays pure — has
                # nothing left to park on. This rung asks the children's own
                # transcripts instead. It reads files, so it lives here on
                # the card path (once per Stop) rather than on the hot one.
                parked = self._async_subagent_reason(session_id)
            if not parked and hook in PARKED_CARD_HOOKS:
                # The fourth rung, and the shell's version of the third: a
                # `Bash` run in the background returns at once and the
                # harness ends the turn, to resume the session itself when
                # the command exits. One Stop in five on this machine ended
                # that way (`background_watch`). The session's own
                # transcript names the task; its output file says whether
                # it has come back.
                parked = self._background_task_reason(session_id)
            if not parked and hook in PARKED_CARD_HOOKS:
                # Mission Control's every reply is quiet by origin: the
                # standing chief-of-staff session answers a question the
                # person typed at it and waits for the next one, which is
                # not a "you're needed" prompt. Without this rung each
                # answer raised the stock card — Needs you, the strip's
                # attention figure, the chime, the phone push, the Live
                # Activity — and the card exempted the row from staleness
                # eviction. Same outcome as a work report: no card, `idle`,
                # the ordinary 300s eviction. Decided off the origin stamp
                # (the state's, else the message's own — the state may not
                # carry it yet on the very first Stop), never the transcript.
                # A Grok message's stamp is the leader's, not the
                # session's (`_probe_grok_origin`): never read it here,
                # or a `grok` typed in Mission Control's terminal would
                # silence every Grok row's Stop for as long as that leader
                # lives.
                quiet = self._mission_reply(
                    session_id,
                    "" if msg.get("provider") == "grok"
                    else str(msg.get("origin") or ""))
            if not parked and not quiet and hook in PARKED_CARD_HOOKS:
                # Not a fifth parking rung: a turn whose closing words are
                # a `## Work done` report with no `bob-tldr` is *finished*,
                # waiting on nobody. No card, no chime, and the state below
                # goes to idle rather than being held — the row sleeps
                # instead of asking. Reads the transcript tail on the loop,
                # once per Stop, for the same reason its siblings do.
                quiet = self._finished_quietly(session_id)
            if parked:
                logger.info(
                    "Suppressing %s card for %s — parked on %s",
                    hook, session_id[:12], parked,
                )
            elif quiet:
                logger.info(
                    "No %s card for %s — the turn ended on a work report "
                    "or Mission Control replied",
                    hook, session_id[:12],
                )
                # An earlier Stop's card for the same session is the same
                # turn's earlier draft: the report supersedes it.
                self._drop_card(session_id)
            else:
                self._raise_card(session_id, msg)
        elif event == "dismiss":
            self._drop_card(session_id)
        elif event == "subagent_start":
            # Defensive against hook-delivery reordering: if a "Waiting for
            # input" Stop/Notification card is already showing for this session,
            # a subagent just came alive, so the parent isn't idle after all.
            # Dismiss the stale card. (A session can only spawn a subagent while
            # running, so this never clears a legitimately idle session's card.)
            self._drop_waiting_card(session_id, "a subagent started")
        elif event in ("tool_use", "tool_done", "tool_failed"):
            # The same argument as subagent_start above, with a much wider proof:
            # the card claims the session is waiting for input, and a tool call
            # says the model is generating. Nothing used to say so. A card was
            # only ever cleared by an explicit `dismiss`, which the hook script
            # sends for UserPromptSubmit and SessionEnd alone — so every
            # resumption that is not a typed prompt (a plan approved, an
            # AskUserQuestion answered, a permission allowed, a parked turn
            # coming back) left the card standing, pinning a busy agent under
            # "Needs you" and holding it in the `waiting` bucket that feeds the
            # strip's attention count and the alert policy. Observed on a live
            # session: four minutes and ~40 tool calls after the card was raised.
            #
            # Two things are deliberately not cleared. A StopFailure card is an
            # API error the human still wants to see, and the retry after it is a
            # tool call — hence PARKED_CARD_HOOKS only. And AskUserQuestion is a
            # tool call that *starts* a wait rather than ending one.
            if not (event == "tool_use" and is_ask_user_question(msg.get("tool_name", ""))):
                self._drop_waiting_card(session_id, f"the session resumed ({event})")

        self._track_pending_question(event, session_id, msg)

        # A SessionStart on a pid that a fresh session already holds is that
        # process's `/clear`, and the earlier session is over.
        incoming_pid = msg.get("pid")
        # Grok under `use_leader` cannot use this. The hook walk either
        # stamps the shared leader or — empirically, the leader being a
        # child of one TUI — some other tab's pid. Either way the number
        # is not unique to this session, and treating it as `/clear`
        # forgets every other live Grok tab. Real Grok `/clear` is
        # `_live_grok_records`' one-pid-one-tab rule. The hook pid still
        # rides into `_update_session_state`; `_ensure_session_pid`
        # replaces it with the roster TUI afterwards.
        dedup_pid = None if msg.get("provider") == "grok" else incoming_pid
        if event == "session_start" and dedup_pid is not None:
            self._forget_cleared_predecessors(session_id, dedup_pid)

        # State transitions are recorded by diffing around this one call rather
        # than from inside _enter_state: several branches below assign `state`
        # directly, so hooking the helper would silently miss them — and a history
        # with holes in it is worse than none.
        state_before = (self._session_states.get(session_id) or {}).get("state")

        changed = self._update_session_state(
            event, hook, session_id,
            msg.get("agent_id", ""), msg.get("tool_name", ""),
            pid=incoming_pid, parked=bool(parked), quiet=quiet,
            subagent_type=msg.get("subagent_type", ""),
            # What started this session, stamped into its environment at the
            # spawn and carried on **every** message the way `key` is — a
            # `SessionStart` sent while the daemon was restarting is dropped
            # for ever, and a start-only stamp would leave exactly that
            # session permanently unattributed.
            # **Never from a Grok hook.** Grok runs its hooks under the
            # shared `grok agent leader`, whose environment is the terminal
            # that first started it, so the stamp on a Grok message names
            # whichever card that terminal was for — one old card on every
            # Grok row since. The session's own process carries the right
            # one; `_probe_grok_origin` below reads it there.
            origin_stamp=("" if msg.get("provider") == "grok"
                          else str(msg.get("origin") or "")),
        )

        # SessionEnd can arrive before we ever stamped provider on the
        # state (or for a roster-only row with no hook state). Remember
        # the id so a stale active_sessions.json entry cannot resurrect it.
        if hook == "SessionEnd" and msg.get("provider") == "grok" and session_id:
            self._grok_ended.add(session_id)

        cur = self._session_states.get(session_id)
        if cur is not None and msg.get("provider") in ("grok", "claude"):
            if cur.get("provider") != msg["provider"]:
                cur["provider"] = msg["provider"]
                changed = True
        if cur is not None and (
            cur.get("provider") == "grok" or msg.get("provider") == "grok"
        ):
            self._probe_grok_origin(session_id, self._ensure_session_pid(session_id, cur))

        self._record_transition(
            session_id, state_before, event, hook, msg.get("tool_name", ""),
        )

        # Take the confetti baton and clear it in one step: _update_session_state
        # returns early for a message with no session_id, so leaving it set would let
        # the next such message re-fire the previous session's burst.

        # Store project name after session state is created. A change is
        # structural — `changed` gates the persist below, and a message whose
        # only news was the project (or cwd) used to leave sessions.json
        # carrying the old one until something else moved.
        if self._claim_project(session_id, project):
            changed = True
        # For a session that already existed, the early claim above took the
        # diff, so the news is carried from there.
        changed = changed or project_changed

        # And the full folder beside it. A message without one (an older
        # installed handler) leaves any stored value alone: absence is silence,
        # not a retraction.
        cwd_claim = msg.get("cwd")
        if cwd_claim and session_id and session_id in self._session_states:
            if self._session_states[session_id].get("cwd") != str(cwd_claim):
                self._session_states[session_id]["cwd"] = str(cwd_claim)
                changed = True

        # And the transcript beside it — the late twin of the early store
        # above, for a session whose entry `_update_session_state` has only
        # just created. `subagent_watch` needs the harness's own string rather
        # than a path rebuilt from the cwd: Claude Code mangles `.` as well as
        # `/`, so a re-derived directory is silently wrong for every project
        # with a dot in its path.
        if transcript_claim and session_id and session_id in self._session_states:
            self._session_states[session_id]["transcript_path"] = \
                transcript_claim

        if self._observers:
            self._notify_observers("on_notification_change", self._notification_snapshot())

        if event != "compact":
            await self._wake_surfaces()

        if changed:
            self._persist_sessions()

        # None for every hook event but the broker's ask, which is waiting on
        # the line for its hold.
        return broker_reply

    @staticmethod
    def _parked_reason(state: Optional[dict]) -> str:
        """Why this session's Stop/Notification is a parked turn rather than a
        request for the human — or "" if it is not parked.

        A string rather than a bool because it is both the log line and the
        decision, and because the reading is worth naming: the parent is stopped
        on its own background agents, so nobody is waiting on you.

        Two reasons here, and two more next door: `_async_subagent_reason`
        and `_background_task_reason` are this test's other half, split off
        because they read transcripts and this must stay pure. All are asked
        at the one card site; none alone is the answer.

        The second of the two is the commoner one. **A session nobody has
        prompted cannot be waiting on you.** Claude Code fires its own
        `idle_prompt` Notification 60s after a session goes quiet, whether or not
        anything was ever asked of it — so `/clear` (which ends the old session
        and starts a fresh id, verified in the log: `SessionEnd reason=clear`
        then `SessionStart source=clear`, 75ms apart) put a brand-new, empty
        session under "Needs you" one minute later, saying "Claude is waiting for
        your input" about a turn that does not exist. Nothing dismisses it either
        — only UserPromptSubmit does, and that is precisely the event that has
        not happened — so it sat there until staleness eviction 5-10 minutes on.
        Opening a terminal and not typing for a minute did the same thing.

        The test is `is False`, not falsy: only `session_start` stamps the key,
        because only a session we watched start can be *known* to be unprompted.
        Sessions restored from `sessions.json`, and every session already alive
        when this shipped, carry no key at all and must read as prompted — the
        alternative is a restart suppressing every genuine wait on the machine.
        """
        if not state:
            return ""
        live = state.get("subagents")
        if live:
            # Bounded by age: hook delivery is best-effort, and a dropped
            # `SubagentStop` used to leave this set populated for the life of
            # the session — parking every one of its cards for ever. A set
            # with no start/stop traffic for SUBAGENT_PARK_MAX_SECONDS no
            # longer parks. An entry with no stamp at all (Grok's
            # `_sync_grok_subagents` replaces the set without one; sessions
            # restored from before the stamp existed) keeps the old
            # behaviour and parks — the bound is for the stream that stamps.
            stamped = state.get("subagent_event_at")
            if stamped is None or \
                    time.time() - stamped <= SUBAGENT_PARK_MAX_SECONDS:
                return f"{len(live)} active subagent(s)"
        if state.get("prompted") is False:
            return "never prompted"
        return ""

    def _async_subagent_reason(self, session_id: str) -> str:
        """`_parked_reason`'s third rung, kept apart because it reads files.

        A session waiting on an **async** subagent looks exactly like one
        waiting on a person: the stop hook already fired, the live set is
        empty, and the parent is quiet. The difference is written down in the
        child's own transcript, so that is what this asks — see
        `subagent_watch` for the measurement and for why a failed read counts
        as *not running*.

        Ids that have finished are dropped as we go, so a session pays for
        each child once and the list empties itself — which is also what
        bounds the file reading here: only the first Stop after a burst of
        spawns walks more than a handful of names.

        **It reads files on the loop, and deliberately.** The bound is
        `MAX_SEEN_SUBAGENTS` × `TAIL_BYTES` per Stop — measured at 27.6ms
        against this machine's largest transcripts, warm, and typically zero
        because the list prunes to the running children on its first pass.
        Hopping it to the executor would put an `await` between reading the
        park reason and acting on it, so a second message for the same
        session could interleave and change the state the card decision was
        made from. Tens of milliseconds is the cheaper of the two.

        A park is stamped as well as returned. Without that, the 300-second
        staleness evictor reaches a correctly parked parent long before
        `subagent_watch.STALE_SECONDS` can release it — a session sitting out
        one silent seven-minute `pytest` run inside its own child would be
        retired to *Recently finished* mid-park, with the ask still to come.
        """
        state = self._session_states.get(session_id)
        if not state:
            return ""
        pending = state.get("subagents_async")
        if not pending:
            return ""
        running = subagent_watch.running_ids(
            state.get("transcript_path", ""), pending)
        if running:
            # `running` already carries the mtime each child was observed at,
            # and storing it back is what stops this being a latch: the next
            # look asks for movement since *this* one, so a child that
            # finishes releases its parent at the next card decision rather
            # than fifteen minutes later.
            state["subagents_async"] = running
            state["async_park_at"] = time.monotonic()
            return f"{len(running)} async subagent(s) still running"
        state.pop("subagents_async", None)
        state.pop("async_park_at", None)
        return ""

    def _background_task_reason(self, session_id: str) -> str:
        """`_parked_reason`'s fourth rung: a Stop that ended a turn on a
        background shell task is not a request for the human.

        Nothing is stored between looks: the transcript is the record of
        what was launched and which notifications have landed, and the
        task's output file is the record of whether it exited, so each card
        decision re-reads both and a finished task releases its session at
        the next one — no latch to expire. `async_park_at` is stamped for
        the same reason `_async_subagent_reason` stamps it: a session
        watching a forty-minute CI run emits nothing, and the 300-second
        evictor would otherwise retire it mid-park.

        Reads files on the loop, like its sibling and for the same reason:
        an `await` between the reading and the card decision would let a
        second message for the session interleave. One 512 KiB tail and a
        4 KiB tail per pending task, once per Stop, and only when the first
        three rungs found nothing to park on.
        """
        state = self._session_states.get(session_id)
        if not state:
            return ""
        running = background_watch.running_tasks(
            state.get("transcript_path", ""))
        if not running:
            return ""
        state["async_park_at"] = time.monotonic()
        return f"{len(running)} background task(s) still running"

    def _mission_reply(self, session_id: str, message_stamp: str = "") -> bool:
        """Whether a Stop / Notification on this session is Mission Control
        answering — the session's origin stamp (`_session_states[sid]["origin"]`,
        first-writer-wins, else the stamp the message itself carries) parses
        to the kind `mission`. Pure over the state and the stamp; reads no
        file. This is not a fourth place the daemon acts on a session: it
        raises nothing, types nothing and delivers nothing — it *withholds*
        a card, exactly as `_parked_reason` and `_finished_quietly` do."""
        state = self._session_states.get(session_id) or {}
        raw = str(state.get("origin") or "") or str(message_stamp or "")
        if not raw:
            return False
        return origin.parse(raw).get("by") == "mission"

    def _finished_quietly(self, session_id: str) -> bool:
        """Whether this session's turn ended on a work report and nothing
        asked — `session_stats.finished_quietly` over the transcript the
        hook named. Claude only: the report hint is never printed to Grok,
        and Codex runs no hook handler. Any doubt is False, which is the old
        behaviour exactly."""
        state = self._session_states.get(session_id)
        if not state:
            return False
        path = str(state.get("transcript_path") or "")
        if not path:
            return False
        try:
            return finished_quietly(path)
        except Exception:  # pragma: no cover - a reader must never raise here
            logger.debug("finished_quietly failed", exc_info=True)
            return False

    async def dismiss_notification(self, session_id: str) -> None:
        """Drop an active notification for good so the next push won't bring it
        back. Shared by the menu-bar banner's Dismiss, the API dismiss action,
        and the wrap-up / low-priority / close-terminal paths. Drops the card (so
        the panel row also disappears), then updates the observer and persists."""
        if session_id not in self._active_notifications:
            return
        self._active_notifications.pop(session_id, None)
        if self._observers:
            self._notify_observers("on_notification_change", self._notification_snapshot())
        await self._wake_surfaces()
        self._persist_sessions()
        logger.info("DISMISS: dropped notification for %s", session_id[:12])

    def _schedule_notification_sound(self) -> None:
        """Arm a single debounced/coalesced chime for newly-surfaced cards.

        A raw chime on every `add` fired for cards the user never actually sees:
        ones dismissed within ~70ms by an immediate UserPromptSubmit, and it
        machine-gunned when several concurrent sessions hit Stop at once. Instead
        we arm one timer; while it's pending, further new cards fold in (a burst
        collapses to one chime). When it fires we play a single chime only if at
        least one card that armed this window is *still* active — so transient
        cards, gone before the timer, chime for nothing. Per-session dedup at the
        call site keeps a re-added card from re-arming it."""
        if self._sound_pending:
            return
        self._sound_pending = True

        async def _fire():
            try:
                await asyncio.sleep(NOTIFICATION_SOUND_DEBOUNCE_SECONDS)
                survivors = self._pending_sound_sessions & self._active_notifications.keys()
                if survivors and self.notification_sound_enabled:
                    _play_notification_sound()
            finally:
                self._sound_pending = False
                self._pending_sound_sessions.clear()
                self._sound_task = None

        self._sound_task = asyncio.create_task(_fire())

    def _notification_snapshot(self) -> list[dict]:
        """A thread-safe, display-ready snapshot of the active notifications.

        Built on the asyncio thread and handed to the observer, so the menu-bar
        (main thread) never iterates _active_notifications directly. Order
        follows _active_notifications insertion order (arrival order).

        A "Waiting for input" card whose session's hysteresis window is still
        open is *withheld*, never dismissed: the panel extracts any row with a
        card into "Needs you", so a card shown while `categorize` still vetoes
        the category would put the row exactly where the hysteresis keeps it out
        of. `_active_notifications` keeps the card (the chime path and the alert
        policy's inputs are untouched), and the re-push timer surfaces it the
        moment the window closes — a genuine wait arrives ~3s late, never not at
        all. Only PARKED_CARD_HOOKS cards are gated: a StopFailure is news."""
        snapshot = []
        withheld: Optional[float] = None
        for sid, msg in self._active_notifications.items():
            if msg.get("hook") in PARKED_CARD_HOOKS:
                remaining = self._waiting_hysteresis_remaining(sid)
                if remaining > 0:
                    withheld = (remaining if withheld is None
                                else min(withheld, remaining))
                    continue
            snapshot.append({
                "session_id": sid,
                "title": msg.get("session_title") or msg.get("project") or "session",
                "message": msg.get("message", ""),
                "hook": msg.get("hook", ""),
                # A StopFailure's machine token (`rate_limit`, ...), so a
                # surface can badge the kind without parsing the sentence.
                "error_kind": str(msg.get("error_kind") or ""),
            })
        if withheld is not None:
            self._arm_hysteresis_repush(withheld)
        return snapshot

    # ── channels ─────────────────────────────────────────────────────────────
    #
    # A channel is a small MCP server Claude Code spawned inside one session
    # (`channel_server.py`), which is the only sanctioned way anything outside
    # that session can put text in front of it. Dark Army's half is deliberately thin:
    # remember where each one is, resolve it to a session lazily, and answer
    # permission prompts. Nothing here invents a session — a channel whose pid
    # Dark Army does not recognise is simply not reachable yet.

    #: Three missed heartbeats. Long enough that a busy machine does not lose a
    #: channel, short enough that a closed session stops being offered as an
    #: address before anyone tries to use it.
    CHANNEL_STALE_SECONDS = 95.0

    def _handle_channel_message(self, msg: dict) -> None:
        port = int(msg.get("port") or 0)
        if not port:
            return None
        if msg.get("type") == "channel_attach":
            known = port in self._channels
            if known:
                # A known port is only trusted to refresh itself: a heartbeat
                # repeats the pid, session id and secret it attached with, so
                # anything else claiming this port is a new claimant — the
                # hook socket authenticates nothing, and overwriting here
                # would hand the incumbent's replies (typed into Dark Army's panel
                # for that session) to whoever sent one JSON line. A live
                # incumbent keeps its port on the same possession rule
                # `_attach_is_displaced` applies across ports; a stale one
                # lapses within CHANNEL_CLAIM_SECONDS, so an honest restart
                # costs a wait, not a permanent refusal.
                held = self._channels[port]
                heartbeat = (
                    int(msg.get("pid") or 0) == int(held.get("pid") or 0)
                    and (str(msg.get("session_id") or "")
                         == str(held.get("session_id") or ""))
                    and (str(msg.get("secret") or "")
                         == str(held.get("secret") or ""))
                )
                if not heartbeat:
                    fresh = time.time() - self.CHANNEL_CLAIM_SECONDS
                    if held.get("last_seen", 0) >= fresh:
                        logger.warning(
                            "Refusing channel_attach on port %d: the port is "
                            "held by a live channel and the claim does not "
                            "match it", port)
                        return None
                    if self._attach_is_displaced(msg):
                        return None
            elif self._attach_is_displaced(msg):
                return None
            self._channels[port] = {
                "port": port,
                "pid": int(msg.get("pid") or 0),
                "cwd": msg.get("cwd") or "",
                # The harness puts the session id in every MCP server's
                # environment, so a channel can simply *say* which session it
                # belongs to. Better than the pid walk it replaces: this is the
                # key sessions are stored under, so there is nothing to resolve.
                # Which variable the channel reads is not cosmetic — see
                # channel_server.SESSION_ID_ENV, where reading the wrong one
                # made every Grok channel claim one dead session.
                "session_id": str(msg.get("session_id") or ""),
                # Immutable for the life of this server process. Old copies do
                # not send it and remain Claude-compatible by default.
                "host": (str(msg.get("host") or channel_server.HOST_CLAUDE)
                         if str(msg.get("host") or channel_server.HOST_CLAUDE)
                         in channel_server.HOSTS else channel_server.HOST_CLAUDE),
                # Whether this server was actually registered as a channel, or
                # is merely the user-scope MCP entry starting in a session that
                # cannot receive anything. See channel_server.is_channel.
                "is_channel": bool(msg.get("is_channel")),
                # The name the channel was registered under, for the dual-name
                # window: a question pushed into this session names the tool
                # it was born with. Copies before the window sent `"bob"` or
                # nothing, and both are the legacy name. Not on any snapshot.
                "name": (msg.get("channel")
                         if msg.get("channel") in channel_server.NAMES
                         else channel_server.LEGACY_NAME),
                # The channel's inbound password. Held only here and in that
                # process — never persisted, never logged, and never sent
                # anywhere but back down the port that announced it. A channel
                # from before this shipped announces none, and gets none back:
                # it is not checking, and an already-open session must not stop
                # working because Dark Army was upgraded underneath it.
                "secret": str(msg.get("secret") or ""),
                "last_seen": time.time(),
            }
            if not known:
                logger.info("Channel attached on port %d (pid %s)",
                            port, msg.get("pid"))
            return None

        # Codex's helper is board-only. Its server already ignores permission
        # notifications; this second guard keeps a forged hook-socket message
        # from turning a Codex registration into a panel permission surface.
        if (self._channels.get(port) or {}).get("host") != channel_server.HOST_CLAUDE:
            return None
        request_id = str(msg.get("request_id") or "")
        if not request_id:
            return None
        session_id = self._channel_session(port) or ""
        self._permission_requests[request_id] = {
            "request_id": request_id,
            "session_id": session_id,
            "port": port,
            "tool_name": str(msg.get("tool_name") or ""),
            # The model's own words about its own tool call, and the arguments
            # it wants to run. Untrusted all the way to whatever renders them.
            "description": str(msg.get("description") or ""),
            "input_preview": str(msg.get("input_preview") or ""),
            "asked_at": time.time(),
        }
        logger.info("Permission relay: %s wants %s (%s)",
                    session_id[:12] or "?", msg.get("tool_name"), request_id)
        self._count_on_session(session_id, "permission_asks")
        self._log_permission(self._permission_requests[request_id],
                             "permission_ask")
        self._schedule_display_push()
        return None

    #: How long an established channel holds its session against a newcomer.
    #: Comfortably over the channel's own 30s heartbeat, so a live incumbent is
    #: never mistaken for a dead one, and under the 95s staleness window, so a
    #: genuinely dead channel is replaceable well before its record expires.
    CHANNEL_CLAIM_SECONDS = 45.0

    def _attach_is_displaced(self, msg: dict) -> bool:
        """Whether this attach is a second port claiming a session that already
        has a live one — in which case it is refused.

        `channel_attach` arrives on the hook socket, which has no authentication
        of any kind: it takes JSON from anything on this machine that can reach
        the port. So the pid, the session id and the port in this message are
        all merely *claimed*, and a process that claimed a session someone else
        already owns would be handed that session's replies — everything the
        user types into Dark Army's panel for it, delivered to the wrong listener and
        never to the right one.

        There is no way to make the claim provable from here, so the guard is
        possession instead: the first channel to claim a session keeps it while
        it keeps heartbeating. A real channel attaches in the same breath as its
        session starts, which is a race an attacker has to win rather than a
        door it can walk through later. A channel that dies stops heartbeating
        and its claim lapses within `CHANNEL_CLAIM_SECONDS`, so the honest
        restart case costs a wait rather than a permanent refusal.

        This does not stop a process that wins that race, and it is not
        pretending to. The one after it — reaching an *established* channel's
        port to speak into the agent — is closed properly, by the password in
        `ChannelServer.listen`.
        """
        session_id = str(msg.get("session_id") or "")
        pid = int(msg.get("pid") or 0)
        if not session_id and not pid:
            return False
        fresh = time.time() - self.CHANNEL_CLAIM_SECONDS
        for other_port, chan in list(self._channels.items()):
            if other_port == int(msg.get("port") or 0):
                continue
            if chan.get("last_seen", 0) < fresh:
                continue
            other_session = str(chan.get("session_id") or "")
            if session_id and other_session:
                # Both sides said who they are, so that is the whole question:
                # two distinct ids are two distinct sessions and the pid below
                # has nothing to add. It has something to get *wrong* — Grok
                # spawns every channel from one shared `grok agent leader`, so
                # a pid match there is the normal case rather than a collision,
                # and treating it as one refused every Grok session but the
                # first a channel of its own.
                same = other_session == session_id
            else:
                # One of them announced nothing, so the pid is all there is.
                # A forger can dodge this by inventing a session id, and gains
                # nothing by it: an id no session carries resolves to no
                # session, and the pid fallback behind it fails closed the
                # moment a pid names more than one row.
                same = bool(pid) and chan.get("pid") == pid
            if same:
                logger.warning(
                    "Refusing channel on port %s: session %s is already held by "
                    "the live channel on port %d",
                    msg.get("port"), session_id[:12] or f"pid {pid}", other_port)
                return True
        return False

    def _live_channels(self) -> dict[int, dict]:
        """Channels that have said something recently. Expiry is by *reading*
        rather than by a timer: a channel goes quiet exactly when its session
        ends, and there is nothing else this daemon needs to do about it."""
        cutoff = time.time() - self.CHANNEL_STALE_SECONDS
        # `list(...)` first, and it is load-bearing. This runs on the executor
        # thread while `_handle_channel_message` writes the same dict on the
        # loop, so iterating it live raised "dictionary changed size during
        # iteration" whenever a channel attached mid-sweep — swallowed as a
        # debug log by the caller, which made the symptom a dropped agents
        # snapshot and a panel frozen for a cycle rather than anything anybody
        # could see. A snapshot of the keys cannot be invalidated by a
        # concurrent insert, and a port that arrives during the sweep is fresh
        # by definition, so skipping it this once costs nothing.
        for port, chan in list(self._channels.items()):
            if chan.get("last_seen", 0) < cutoff:
                self._channels.pop(port, None)
                logger.info("Channel on port %d went quiet", port)
        return dict(self._channels)

    def _channel_session(self, port: int, *, require_known: bool = True) -> Optional[str]:
        """Which session a channel belongs to, resolved at the moment of use.

        By pid, and only by pid: a channel's parent *is* the harness process,
        which is the key every session here is already keyed on. Resolving
        lazily rather than at attach matters — a channel starts with its session
        and may well announce itself before the session's first hook event has
        told Dark Army that the session exists at all.

        ``require_known`` (the default) is for *talking to* a session — a reply,
        a wrap-up, a permission prompt. An id Dark Army has never hooked names a row
        nothing can be drawn on, so it is refused and the pid fallback runs.

        Board tools pass ``require_known=False``. Measured live under Grok's
        shared leader: each MCP child announces a unique ``GROK_SESSION_ID``
        while Dark Army's hook table often holds only one of those sessions (the
        TUI that started the leader). Requiring the id to already be in
        ``_session_states`` then refuses every other Grok window's
        ``dark_army_add_card``. A Prep card attributed to an unhooked id starts
        nothing; a misattributed reply into the wrong session would.
        """
        entry = self._channels.get(port)
        if not entry:
            return None
        # What the channel said about itself, when it knew. Push/permission
        # only trust it for a session Dark Army actually has. Board tools take the
        # announced id as the author even before the first hook, because the
        # alternative under a shared parent pid is "could not tell which
        # session asked".
        sid = entry.get("session_id") or ""
        if sid and (not require_known or sid in self._session_states):
            return sid
        if not entry.get("pid"):
            return None
        # The fallback, for a harness that announces no id at all. It resolves
        # only when the pid names **one** session, and that is not pedantry:
        # a channel's parent is normally its own harness process, but Grok's is
        # the shared `grok agent leader`, so every Grok session on the machine
        # carries the same pid here. Returning the first match would hand one
        # session's card — or one session's reply — to whichever row happened to
        # be earliest in a dict. Ambiguity fails closed, which costs an
        # unattributed request and never a misattributed one.
        matches = [sid for sid, state in self._session_states.items()
                   if state.get("pid") == entry["pid"]]
        return matches[0] if len(matches) == 1 else None

    def _codex_channel_session(self, port: int) -> Optional[str]:
        """Resolve a board-only Codex MCP process without guessing its author.

        The MCP server's parent is the Codex process Dark Army already attached to a
        rollout record. Exact PID wins. The fallback is a unique top-level,
        active-looking rollout in the same normalised cwd; child records never
        qualify. ``_codex_records`` is rebound by the executor refresh, so bind
        one local snapshot before either pass.
        """
        entry = self._channels.get(port)
        if not entry or entry.get("host") != channel_server.HOST_CODEX:
            return None
        records = dict(self._codex_records)
        roots = [record for record in records.values()
                 if not record.is_child]
        pid = int(entry.get("pid") or 0)
        if pid:
            matches = [record.session_id for record in roots if record.pid == pid]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        cwd = str(entry.get("cwd") or "")
        if not cwd:
            return None
        normal = dispatch.normalise_root(cwd)
        matches = [record.session_id for record in roots
                   if record.cwd
                   and dispatch.normalise_root(record.cwd) == normal]
        return matches[0] if len(matches) == 1 else None

    def _board_request_session(self, port: int) -> Optional[str]:
        """Host-aware attribution for every inbound board verb.

        Codex is resolved from the rollout record. Claude and Grok take the
        id the channel announced, even when Dark Army has not hooked that session
        yet — see ``_channel_session(require_known=False)``.
        """
        entry = self._channels.get(port)
        if not entry:
            return None
        if entry.get("host") == channel_server.HOST_CODEX:
            return self._codex_channel_session(port)
        return self._channel_session(port, require_known=False)

    def _channel_for_session(self, session_id: str) -> Optional[dict]:
        """A channel Dark Army can actually push through, or None.

        `is_channel` is the whole point of this filter. Registration is
        user-scope, so the server starts in *every* session and announces
        itself from all of them — two ordinary sessions reported themselves
        reachable the first time this shipped. Only a session started with the
        flag can receive anything; the rest have the harness drop the event in
        silence, which is the worst possible failure for a button.
        """
        for port, entry in self._live_channels().items():
            # From the returned copy, not re-indexed into `_channels`: the port
            # can be gone by the next statement, and that subscript would raise.
            if not entry.get("is_channel"):
                continue
            if self._channel_session(port) == session_id:
                return entry
        return None

    async def _send_to_channel(self, port: int, payload: dict) -> bool:
        """One connection, one line — the shape every other sender here uses.

        The line carries the password that channel announced, which is what
        distinguishes us from anything else on the machine that can open a
        socket to a loopback port. Read at the moment of sending rather than
        passed in by the caller: the registry is the only thing that knows
        which secret belongs to which port, and a caller that had to carry one
        could get the pairing wrong.
        """
        payload = dict(payload)
        payload["secret"] = (self._channels.get(port) or {}).get("secret", "")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=2.0)
        except (OSError, TimeoutError):
            logger.info("Channel on port %d did not answer", port)
            self._channels.pop(port, None)
            return False
        try:
            writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            await writer.drain()
            return True
        except OSError:
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def push_channel_event(self, session_id: str, content: str,
                                 meta: Optional[dict] = None) -> bool:
        """Put one line in front of a live session. False if it is unreachable.

        Unreachable is the ordinary case, not an error: a session is reachable
        only if it was *started* with Dark Army's channel, and most are not.
        """
        channel = self._channel_for_session(session_id)
        if channel is None:
            return False
        return await self._send_to_channel(
            channel["port"], {"type": "event", "content": content,
                              "meta": meta or {}})

    #: The longest reply Dark Army will carry. A panel field is for "accept", "no, use
    #: SQLite", "run the tests first" — the answers you give without leaving
    #: what you were doing. A prompt worth more than this is worth the terminal,
    #: where you can see what you are typing into.
    MAX_REPLY_CHARS = 2000

    #: `message_card`'s length refusal, and the seventh of the
    #: `CARD_MESSAGE_*` family at the top of this module. It lives here rather
    #: than beside its six siblings so the number is written once: a
    #: module-level copy would drift from the limit above the first time
    #: somebody changed one of them.
    CARD_MESSAGE_TOO_LONG_REFUSAL = (
        f"That message is longer than {MAX_REPLY_CHARS} characters — say it "
        "in the terminal, where you can see what you are typing.")

    async def reply_to_session(self, session_id: str,
                               text: str) -> tuple[bool, str]:
        """Say something to a live session, as the user.

        This works for the wait that *ends a turn* — "Plan ready. accept?" —
        where the session is idle rather than blocked, and an arriving event
        wakes a turn. It cannot work for `AskUserQuestion` or plan approval:
        those block inside a tool on terminal input, and an event queues behind
        the very dialog it would answer. Dark Army does not offer it there.

        Refusals are returned rather than logged, because the failure that
        matters is invisible: a session with no channel swallows the push
        without a word, and a reply box that silently does nothing is worse than
        no reply box.
        """
        if self._session_provider(session_id) == "codex" or session_id.startswith("codex:"):
            ok, detail = await self._reply_native_codex(session_id, text)
            capture = getattr(self, "_decisions", None)
            episode = capture.session_items.get(session_id) if capture else None
            if ok and capture and episode:
                capture.submit("finish", "question:" + session_id, "delivered_unconfirmed",
                               text, "reply submitted to Codex terminal", episode_id=episode)
            return ok, detail
        text = (text or "").strip()
        if not text:
            return False, "Nothing to send."
        if len(text) > self.MAX_REPLY_CHARS:
            return False, f"Too long — {self.MAX_REPLY_CHARS} characters max."
        capture = getattr(self, "_decisions", None)
        episode = capture.session_items.get(session_id) if capture else None
        if self._session_provider(session_id) == "grok":
            if not self._grok_leader_up:
                return False, ("The Grok leader is not running, "
                               "so this session cannot be reached from here.")
            if session_id not in self._grok_resident:
                return False, ("That session is not attached to the Grok leader, "
                               "so it cannot be reached from here.")
            try:
                ok, detail = await self._leader.prompt(session_id, text)
            except Exception:
                logger.warning("Grok leader prompt failed", exc_info=True)
                return False, "That session is no longer listening."
            if not ok:
                return False, detail or "That session is no longer listening."
            logger.info("Replied to %s via grok leader (%d chars)",
                        session_id[:12], len(text))
            if capture and episode:
                capture.submit("finish", "question:" + session_id, "delivered_unconfirmed", text, "reply sent to Grok leader", episode_id=episode)
            return True, ""
        if self.typed_reply_enabled:
            # Typed first, channel as the fallback. A typed refusal on a
            # session that *has* a channel is logged and the push goes ahead;
            # on one without, the typed sentence is the answer, because it
            # names what is missing (a stopped row, one line, a VS Code
            # window) where the channel refusal below could only say "not
            # started with the channel".
            ok, detail = await self._reply_by_typing(session_id, text)
            if ok:
                if capture and episode:
                    capture.submit("finish", "question:" + session_id, "delivered_unconfirmed", text, "reply typed into terminal", episode_id=episode)
                return True, ""
            if self._channel_for_session(session_id) is None:
                return False, detail
            logger.info("Typed reply to %s refused (%s); falling back to the channel",
                        session_id[:12], detail)
        if self._channel_for_session(session_id) is None:
            return False, ("That session was not started with Dark Army's channel, "
                           "so it cannot be reached from here.")
        # `kind="user"` is what tells the agent this is its own person typing,
        # rather than Dark Army reporting something — see the channel's instructions.
        ok = await self.push_channel_event(session_id, text, {"kind": "user"})
        if not ok:
            return False, "That session is no longer listening."
        logger.info("Replied to %s (%d chars)", session_id[:12], len(text))
        if capture and episode:
            capture.submit("finish", "question:" + session_id, "delivered_unconfirmed", text, "reply sent to channel", episode_id=episode)
        return True, ""

    def _codex_reply_candidate(self, session_id):
        """Snapshot eligibility only; every write repeats exact-holder observation."""
        record = self._codex_records.get(session_id)
        if record is None or record.is_child:
            return None, "Reply in the original Codex session; this is not an interactive root."
        if not self.typed_reply_enabled:
            return None, ("Replies to Codex are off in Settings. Enable replies through the terminal "
                          "to reply here, or answer in the original Codex session.")
        # A queued `request_user_input_async` question on a finished turn is
        # answered by the person's next ordinary message — the parser releases
        # it on a real user reply — so the reply route stays open for it and
        # the row's reply box and choice words become the answer. A
        # synchronous question is a live picker in the terminal, where a typed
        # line and its Enter would land on the highlighted option: still
        # refused. So is a permission prompt or a hook-held question.
        queued = record.question_async
        if (((record.stats.question or record.stats.questions or self._questions.get(session_id))
                and not queued)
                or self._pending_questions.get(session_id) or session_id in self._prompts_by_session()):
            return None, "Dark Army can show this Codex question. Answer it in the original Codex session."
        if codex_terminal.roots((record,)):
            expected = codex_input.candidate(record)
            proof = getattr(self, "_codex_input_proofs", {}).get(session_id)
            if (expected is None or proof is None or proof.candidate != expected
                    or time.monotonic() - proof.observed_at > codex_input.MAX_AGE_SECONDS
                    or session_id in self._session_states or session_id in self._agent_records
                    or session_id in self._grok_records):
                return None, "Codex has not confirmed that this thread can receive a reply. Waiting for its local server."
            if self._codex_reply_attempts.get(session_id) == (record.thread_id, record.turn_id):
                return None, "A reply was already submitted for this turn; check the original Codex session before sending again."
            return proof, ""
        if not codex_rollouts.stopped_turn(record, awaiting_answer=queued):
            return None, "That Codex turn is still active or its completion is unverified. Reply in the original Codex session."
        proof = self._codex_human_close_candidate(session_id, awaiting_answer=queued)
        if proof is None:
            return None, "The Codex terminal or its helpers cannot be verified. Reply in the original Codex session."
        key = (record.thread_id, record.turn_id)
        if self._codex_reply_attempts.get(session_id) == key:
            return None, "A reply was already submitted for this turn; check the original Codex session before sending again."
        connection, detail = vscode_reveal.native_reply_connection(proof.pid, proof.root.cwd)
        if connection is None:
            return None, detail
        return proof, ""

    async def _reply_native_codex(self, session_id, text):
        text = vscode_reveal.native_reply_text(text)
        if text is None:
            return False, "Use one short plain line, without terminal commands or control characters."
        captured = self._navigation_capture()
        expected, detail = self._codex_reply_candidate(session_id)
        if expected is None:
            return False, detail
        if session_id in self._answering:
            return False, "Already submitting a reply to this session. Check its terminal."
        if isinstance(expected, codex_input.Proof):
            return await self._reply_shared_codex(session_id, text, expected)
        record = captured[0][session_id]
        turn_id = record.turn_id
        # Only a queued async question may stand while the reply goes in;
        # the fresh re-parse below applies the same rule to its own reading.
        queued = record.question_async
        children = tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                         for r in self._refinement_descendants(record))
        key = (record.thread_id, turn_id)

        def current():
            descendants = self._refinement_descendants(record)
            return (self._navigation_capture_current(captured)
                    and record.turn_id == turn_id and descendants is not None
                    and tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                              for r in descendants) == children
                    and self._codex_reply_candidate(session_id)[0] == expected)

        async def validate(*, consume=False):
            if not current():
                return False
            def observe():
                return codex_rollouts.refinement_close_observation(
                    tuple(r.root for r in captured[1]), expected.root,
                    expected.journal, turn_id, children, require_stopped=True,
                    awaiting_answer=queued)
            fresh = await asyncio.get_running_loop().run_in_executor(None, observe)
            if fresh is None or fresh != expected or not current():
                return False
            if consume:
                # No await from this claim to the asyncio transport's write.
                self._codex_reply_attempts[session_id] = key
            return True

        async def before_write():
            return await validate(consume=True)

        self._answering.add(session_id)
        expires_at_ms = int((time.time() + REFINEMENT_CLOSE_TIMEOUT) * 1000)
        try:
            async with asyncio.timeout(REFINEMENT_CLOSE_TIMEOUT):
                async with self._dispatch_lock, self._board_write_lock:
                    if not await validate():
                        return False, "The Codex session or exact terminal changed. Nothing was sent."
                    result = await vscode_reveal.reply_native_terminal(
                        expected.pid, expected.tty, expected.root.cwd, text, before_write,
                        expires_at_ms=expires_at_ms)
                    if not (isinstance(result, dict) and result.get("matched") is True
                            and result.get("sent") is True):
                        refusal = _typed_nothing_refusal(result)
                        if refusal:
                            # The window refused in words before typing
                            # anything (0.1.21+ in a folder VS Code has not
                            # trusted): nothing reached the turn, so the
                            # claim `before_write` took is released and the
                            # person may try again once the folder is trusted.
                            if self._codex_reply_attempts.get(session_id) == key:
                                self._codex_reply_attempts.pop(session_id, None)
                            return False, refusal
                        return False, "The editor did not confirm submission. Check the original Codex session; no retry was sent."
                    return True, ""
        except TimeoutError:
            return False, "Codex reply timed out. Check the original Codex session; no retry was sent."
        finally:
            self._answering.discard(session_id)
            self._schedule_agents_push()

    async def _reply_shared_codex(self, session_id, text, expected):
        key = (expected.candidate.root.thread_id, expected.candidate.turn_id)

        def current():
            proof, _ = self._codex_reply_candidate(session_id)
            return (isinstance(proof, codex_input.Proof)
                    and proof.candidate == expected.candidate
                    and proof.peer == expected.peer and proof.socket == expected.socket)

        def consume():
            if self._codex_reply_attempts.get(session_id) == key:
                return False
            self._codex_reply_attempts[session_id] = key
            return True

        self._answering.add(session_id)
        try:
            return await codex_input.submit(expected, text, current, consume)
        finally:
            self._answering.discard(session_id)
            self._schedule_agents_push()

    def _refresh_codex_input(self):
        # With replies off Dark Army never talks to Codex's server at all
        # (`docs/codex-contract.md`), and a proof from before stays unused.
        if not self.typed_reply_enabled:
            self._codex_input_proofs, self._codex_input_roots = {}, None
            return
        task = getattr(self, "_codex_input_task", None)
        if task is not None and not task.done():
            return
        # Sorted like `codex_terminal.roots`: activity reorders the roster
        # without changing what there is to read.
        captured = tuple(sorted(
            (c for r in self._codex_records.values()
             if (c := codex_input.candidate(r)) is not None),
            key=lambda c: c.root.session_id))
        now = time.monotonic()
        if (captured == getattr(self, "_codex_input_roots", None)
                and now - getattr(self, "_codex_input_at", 0) < codex_input.REFRESH_SECONDS):
            return
        self._codex_input_roots, self._codex_input_at = captured, now

        async def observe():
            proofs = await codex_input.observe(captured)
            previous = getattr(self, "_codex_input_proofs", {})
            self._codex_input_proofs = {
                sid: p for sid, p in proofs.items()
                if codex_input.candidate(self._codex_records.get(sid)) == p.candidate}
            # Freshness alone buys no frame; only a changed capability does.
            def capabilities(values):
                return {sid: (p.candidate, p.peer, p.socket) for sid, p in values.items()}
            if capabilities(previous) != capabilities(self._codex_input_proofs):
                self._schedule_agents_push()

        self._codex_input_task = asyncio.create_task(observe())

    async def _reply_by_typing(self, session_id: str,
                               text: str) -> tuple[bool, str]:
        """Put `text` on the session's own input line and press Enter for it.

        The second delivery route for `reply_to_session`, behind
        `typed_reply_enabled`: Ctrl-U, the words, one newline, through
        `session_io.send_text` — `wrap_up_session`'s bytes with prose in
        place of the slash command. What the channel buys that this cannot is
        a push *during* a turn; what this buys is a reply box on a session
        that was never started with the channel command.

        Every check runs before the first keystroke and a refusal never
        types, because the trailing Enter lands on whatever the TUI is
        showing: a permission dialog would read it as "confirm", a question
        dialog as "pick the highlighted option", a second line as a second
        message. Order and wording are the plan's
        (`plans/2026-09-05-reply-through-vscode-typing.md`, step 5). A
        multi-line reply is refused rather than collapsed — rewriting the
        person's words silently is worse than a refusal — and there is no
        bracketed paste, an untested assumption about the client.
        """
        if self._session_provider(session_id) == "codex":
            return False, "Dark Army cannot type into a Codex session."
        if "\n" in text or "\r" in text:
            return False, ("One line at a time — the terminal sends each line as "
                           "its own Enter. Shorten it to one line, or start the "
                           "session with Dark Army's channel.")
        if text[0] in TYPED_REPLY_REFUSED_PREFIXES:
            return False, (f"Starts with '{text[0]}', which the terminal would "
                           "run as a command rather than send as words.")
        if session_id in self._prompts_by_session():
            return False, PROMPT_BLOCKED_REFUSAL
        if self._questions.get(session_id):
            return False, ("That session is waiting on a question — "
                           "use the option buttons.")
        if session_id in self._answering:
            return False, ("Already typing an answer into that session — "
                           "wait for it to finish.")
        category = self._reconciled_categories().get(session_id)
        if category is None:
            return False, "Dark Army has no state for that session."
        if category not in ("waiting", "sleeping"):
            return False, "That session is still working — wait for it to stop."
        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if not pid:
            return False, "No PID on record for this session."
        if not session_io.can_send_text(pid):
            return False, ("That session is not in a VS Code window Dark Army can "
                           "type into (Dark Army's extension 0.1.6 or newer).")
        # One burst per session, shared with the question answerers: two
        # writers interleaving keystrokes on one input line is the failure
        # neither could detect.
        self._answering.add(session_id)
        try:
            result = await session_io.send_text(
                pid, "", INPUT_LINE_CLEAR + text, newline=True)
        finally:
            self._answering.discard(session_id)
        if not (result and result.get("sent")):
            return False, _typed_nothing_refusal(result) or NO_TYPING_WINDOW_REFUSAL
        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Replied to %s by typing via %s (%d chars)",
                    session_id[:12], where, len(text))
        return True, ""

    async def wrap_up_session(self, session_id: str) -> tuple[bool, str]:
        """Acknowledge a finished turn and clear the session for the next one.

        Two acts, in this order and only together: `/clear` is typed onto the
        session's input line, and the notification card is dropped. The card
        goes second because it is the cheap half — dismissing first and then
        failing to type would take the row out of "Needs you" with the session
        still full, which is the one outcome nobody asked for.

        Grok is refused here, not forgotten. `/clear` starts a new Grok id
        (`source=new`) and never SessionEnds the old one, so typing it and
        dismissing the card used to leave an `idle` leftover that `categorize`
        banks under "Needs you" — measured 17 Aug on 01a010bc. Forgetting that
        leftover as `cleared` vanished the row a person still needed to read.
        The leftover now sleeps with its `last_report` and Close; this verb
        types nothing. A stale `wrap_up` action's close-failure fallback
        (`wrap_up_or_close_session`) lands here too.

        The bytes go through VS Code's `sendText`, exactly as auto-compact does
        and for the same reason: a slash command is expanded by the client on
        the input line, before any request exists, so the channel cannot carry
        it — see `autocompact`. That also fixes the reach: a session outside a
        VS Code window with the 0.1.6+ extension cannot be wrapped up, and says
        so rather than pretending. The panel only draws the button where
        `can_type` is true, so this refusal is the race, not the common case.

        `/clear` is not undoable and it is not idempotent-shaped either — a
        second one clears whatever was said since. The confirm that guards it
        lives on the panel (arm, then press again), the same gate Stop and
        Retire carry. The board's Done leg no longer clears, it closes
        (`_wrap_up_for_done`).
        """
        if (self._session_provider(session_id) == "grok"
                or session_id in self._grok_records):
            return False, GROK_WRAP_UP_REFUSAL
        if session_id in self._prompts_by_session():
            # Same trap `_flush_auto_compacts` documents: a relayed tool-approval
            # dialog owns the input line and `sendText` ends in a newline, which
            # the dialog reads as "confirm the highlighted choice". Wrapping up
            # must never approve a tool call on the way past.
            return False, PROMPT_BLOCKED_REFUSAL

        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if not pid:
            return False, "No PID on record for this session."

        result = await session_io.send_text(
            pid, "", INPUT_LINE_CLEAR + WRAP_UP_COMMAND)
        if not (result and result.get("sent")):
            return False, _typed_nothing_refusal(result) or NO_TYPING_WINDOW_REFUSAL

        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Wrapped up %s via %s", session_id[:12], where)
        await self.dismiss_notification(session_id)
        return True, ""

    def _refinement_root_capture(self, session_id):
        record = self._codex_records.get(session_id)
        roots = codex_rollouts.project_title_roots((record,)) if record else ()
        if len(roots) != 1 or not record.turn_id:
            return None
        return record, roots[0], record.turn_id

    def _prune_refinement_receipts(self):
        now = time.monotonic()
        for sid, receipt in list(self._refinement_receipts.items()):
            captured = self._refinement_root_capture(sid)
            if (receipt.expires <= now or not captured
                    or captured[1:] != (receipt.root, receipt.turn_id)
                    or self._closed_by_bob(sid) or sid in self._hidden_codex):
                self._refinement_receipts.pop(sid, None)

    def _remember_refinement_attachment(self, session_id, attached, captured,
                                       project_root, journal):
        self._prune_refinement_receipts()
        if (not captured or not journal
                or self._refinement_root_capture(session_id) != captured
                or self._codex_records.get(session_id) is not captured[0]
                or captured[1].cwd != project_root):
            return
        previous = self._refinement_receipts.get(session_id)
        if previous is not None:
            # Neither a second card nor reattachment renews spent authority.
            if previous.card_id != attached["id"]:
                self._refinement_receipts[session_id] = replace(previous, state="ambiguous")
            return
        if len(self._refinement_receipts) >= 500:
            return
        self._refinement_receipts[session_id] = RefinementCloseReceipt(
            str(attached["id"]), captured[1], project_root,
            str(attached["plan_path"]), journal, captured[2],
            float(attached["updated_at"]), time.monotonic() + REFINEMENT_CLOSE_SECONDS)

    def _refinement_descendants(self, record):
        """Capture the whole known subtree, including children missing in stats."""
        selected = {record.thread_id}
        descendants = []
        remaining = list(self._codex_records.values())
        while True:
            found = [r for r in remaining if r.parent_thread_id in selected]
            if not found:
                return tuple(descendants)
            for child in found:
                if child.thread_id in selected:
                    return None  # duplicate/cyclic ownership cannot grant close
                selected.add(child.thread_id)
                descendants.append(child)
                remaining.remove(child)

    def _refinement_busy(self, record, *, awaiting_answer=False):
        finished = {r.thread_id for r in self._codex_records.values()
                    if r.parent_thread_id == record.thread_id and r.turn_id
                    and not r.turn_active and not r.stats.question and not r.stats.questions}
        return (codex_rollouts.question_blocks(record, awaiting_answer)
                or codex_rollouts.has_unsettled_path_children(record, self._codex_records.values())
                or any(a.activity not in ("completed", "shutdown", "errored") and aid not in finished
                       for aid, a in record.stats.agents.items()))

    def _refinement_current(self, sid, receipt, captured, children):
        record = self._codex_records.get(sid)
        descendants = self._refinement_descendants(record) if record else None
        return (self._refinement_receipts.get(sid) is receipt
                and receipt.state == "ready" and receipt.expires > time.monotonic()
                and self._navigation_capture_current(captured)
                and record is not None
                and self._refinement_root_capture(sid) is not None
                and self._refinement_root_capture(sid)[1:] == (receipt.root, receipt.turn_id)
                and not self._closed_by_bob(sid) and sid not in self._hidden_codex
                and sid not in self._session_states and sid not in self._agent_records
                and sid not in self._grok_records
                and sid not in self._prompts_by_session()
                and not self._pending_questions.get(sid)
                and not self._refinement_busy(record)
                and descendants is not None
                and tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                          for r in descendants) == children
                and not any(r.turn_active or not r.turn_id or self._refinement_busy(r)
                            for r in descendants))

    async def close_refinement_terminal(self, session_id: str) -> tuple[bool, str]:
        """Close one completed planning handoff; running is legal, signals are not."""
        if not isinstance(session_id, str) or not session_id.startswith("codex:"):
            return False, "Only a Codex planning session can request this close."
        self._prune_refinement_receipts()
        receipt = self._refinement_receipts.get(session_id)
        if receipt is None or receipt.state != "ready":
            return False, "No unexpired, unused plan attachment permits this close. Left open."
        captured = self._navigation_capture()
        descendants = self._refinement_descendants(captured[0][session_id])
        if descendants is None:
            return False, "The planning session's helper ancestry is ambiguous. Left open."
        children = tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                         for r in descendants)
        sent = False
        try:
            async with asyncio.timeout(REFINEMENT_CLOSE_TIMEOUT):
                # Start and writes cannot overtake final validation/disposal.
                # This also serializes duplicate requests, without a new lock map.
                async with self._dispatch_lock, self._board_write_lock:
                    if not self._refinement_current(session_id, receipt, captured, children):
                        return False, "The planning session changed or still needs attention. Left open."

                    async def validate():
                        if not self._refinement_current(session_id, receipt, captured, children):
                            return None
                        def observe():
                            store = self._board

                            def scope_clear():
                                # A successful attachment grants no authority to
                                # end implementation or refinement of another card.
                                # These indexed reads run on this worker, under the
                                # action's dispatch/board locks, and repeat after
                                # the scan because reconcile also binds on a worker.
                                return (store is not None and self._board is store
                                        and not any(c.get("column_name") != "done"
                                                    for c in store.by_session(session_id))
                                        and not any(c.get("id") != receipt.card_id
                                                    and c.get("refine_state") in ("dispatching", "live")
                                                    for c in store.by_refine_session(session_id)))

                            if not scope_clear():
                                return None
                            card = store.get(receipt.card_id)
                            if (not card or card.get("column_name") != "backlog"
                                    or card.get("updated_at") != receipt.card_updated_at
                                    or card.get("plan_path") != receipt.plan_path
                                    or os.path.realpath(card.get("root") or "") != receipt.project_root
                                    or card.get("session_id") or card.get("link_state")
                                    or card.get("refine_state")
                                    or card.get("refine_session_id") != session_id):
                                return None
                            resolved, refusal = self._plan_path_refusal(receipt.project_root, receipt.plan_path)
                            if refusal or resolved != receipt.plan_path:
                                return None
                            proof = codex_rollouts.refinement_close_observation(
                                tuple(r.root for r in captured[1]), receipt.root,
                                receipt.journal, receipt.turn_id, children)
                            # A worker-side observer can write the store while
                            # loop-owned locks are held; reject any intervening edit.
                            current = store.get(receipt.card_id)
                            return proof if current == card and scope_clear() else None
                        proof = await asyncio.get_running_loop().run_in_executor(None, observe)
                        return proof if self._refinement_current(session_id, receipt, captured, children) else None

                    proof = await validate()
                    if proof is None:
                        return False, "The attached card or exact planning terminal could not be verified. Left open."

                    async def final_validation():
                        nonlocal sent
                        fresh = await validate()
                        if fresh != proof or fresh is None:
                            return False
                        # Consume before transport; an unanswered POST must never retry.
                        self._refinement_receipts[session_id] = replace(receipt, state="consumed")
                        sent = True
                        return True

                    reply = await vscode_reveal.close_refinement_terminal(
                        proof.pid, proof.tty, receipt.project_root, final_validation)
                    if not (isinstance(reply, dict) and reply.get("matched") is True
                            and reply.get("closed") is True):
                        # Old bridges and refusals are terminal outcomes too.
                        if self._refinement_receipts.get(session_id) is receipt:
                            self._refinement_receipts[session_id] = replace(receipt, state="consumed")
                        return False, ("The editor did not confirm this planning terminal closed. "
                                       "It may still be open; no retry or clear was sent.")
                    self._note_closed(session_id, pid=proof.pid, create_time=proof.create_time)
                    self._settle_codex_stop(session_id, "closed", observed=captured[0][session_id])
                    return True, "Planning terminal closed; the attached plan stays in Backlog."
        except TimeoutError:
            if sent:
                return False, "The close reply timed out; the terminal may have closed. No retry was sent."
            return False, "Planning terminal validation timed out. Left open."

    def _codex_human_close_candidate(self, session_id, *, awaiting_answer=False):
        """Cheap snapshot facts; no new journal parsing or process scan per row.

        `awaiting_answer` is passed by the reply route only (see
        `codex_rollouts.stopped_turn`); a close never passes it.
        """
        record = self._codex_records.get(session_id)
        proof = self._navigation_proof_current(session_id)
        if proof is None and not awaiting_answer:
            proof = self._codex_terminal_current(session_id)
        if (record is None or proof is None or record.is_child
                or not codex_rollouts.stopped_turn(record, awaiting_answer=awaiting_answer)
                or session_id in self._session_states or session_id in self._agent_records
                or session_id in self._grok_records or session_id in self._prompts_by_session()
                or self._pending_questions.get(session_id)
                or self._refinement_busy(record, awaiting_answer=awaiting_answer)):
            return None
        descendants = self._refinement_descendants(record)
        if descendants is None or any(not codex_rollouts.stopped_turn(child)
                                      or self._refinement_busy(child) for child in descendants):
            return None
        return proof

    async def _close_native_codex_by_person(self, session_id):
        """Human stopped-only authority; never a plan receipt or Stop permission."""
        captured = self._navigation_capture()
        expected = self._codex_human_close_candidate(session_id)
        if expected is None:
            return False, "This Codex session is still active or its finished terminal cannot be verified. Left open."
        record = captured[0][session_id]
        turn_id = record.turn_id
        descendants = self._refinement_descendants(record)
        children = tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                         for r in descendants)

        def current():
            fresh_children = self._refinement_descendants(record)
            return (self._navigation_capture_current(captured)
                    and record.turn_id == turn_id
                    and fresh_children is not None
                    and tuple((r.thread_id, Path(r.path), r.parent_thread_id, r.turn_id)
                              for r in fresh_children) == children
                    and self._codex_human_close_candidate(session_id) == expected)

        async def validate():
            if not current():
                return False
            if isinstance(expected, codex_terminal.Target):
                before = await asyncio.to_thread(
                    codex_terminal.stopped_journals, expected.root, turn_id, children)
                if before is None or not current():
                    return False
                # Every shared root, not only this one: a second thread shown
                # in the same terminal withdraws both, so Close never ends a
                # terminal that is running another thread's turn.
                fresh = await codex_terminal.resolve(
                    codex_terminal.roots(self._codex_records.values()),
                    recheck=True, stopped_turns={session_id: turn_id})
                after = await asyncio.to_thread(
                    codex_terminal.journal_revisions, expected.root, children)
                return (fresh.get(session_id) == expected and before == after and current())
            def observe():
                return codex_rollouts.refinement_close_observation(
                    tuple(r.root for r in captured[1]), expected.root,
                    expected.journal, turn_id, children, require_stopped=True)
            fresh = await asyncio.get_running_loop().run_in_executor(None, observe)
            return fresh == expected and fresh is not None and current()

        shared = isinstance(expected, codex_terminal.Target)
        sent = False
        try:
            async with asyncio.timeout(SHARED_CODEX_CLOSE_TIMEOUT if shared
                                       else REFINEMENT_CLOSE_TIMEOUT):
                async with self._dispatch_lock, self._board_write_lock:
                    if not await validate():
                        return False, "The Codex session or exact terminal changed. Left open."
                    sent = True
                    reply = await vscode_reveal.close_refinement_terminal(
                        expected.pid, expected.tty, expected.root.cwd, validate)
                    if not (isinstance(reply, dict) and reply.get("matched") is True
                            and reply.get("closed") is True):
                        logger.info("Codex close for %s refused by the editor: %s",
                                    session_id[:16], (reply or {}).get("reason")
                                    if isinstance(reply, dict) else "no reply")
                        return False, ("The editor could not confirm this Codex terminal closed. "
                                       "It may still be open; no retry or clear was sent.")
                    created = expected.created if shared else expected.create_time
                    self._note_closed(session_id, pid=expected.pid, create_time=created)
                    if shared:
                        self._closed_codex_turns.setdefault(session_id, turn_id)
                    self._settle_codex_stop(session_id, "closed", observed=record)
                    return True, ""
        except TimeoutError:
            if not sent:
                return False, "Checking this Codex terminal took too long; nothing was sent. Left open."
            return False, "Codex terminal close timed out; it may still be open. No retry was sent."

    async def close_session_terminal(self, session_id: str, *,
                                     by_person: bool = False) -> tuple[bool, str]:
        """Close this session's VS Code terminal tab. `(ok, person-readable)`.

        The panel's wrap-up button. `_close_session_terminal` does the act;
        this translates its log-only details and refuses a session holding an
        open permission prompt — a mid-turn ask, not finished reading.
        `wrap_up_session` is not called from here: typing `/clear` is
        pointless once the tab is gone.

        The keyword-only flag is the caller's assertion that a human pressed
        a button, and it is the *only* thing that finishes the board card.
        `close-out.sh` sends the same `close_terminal` action only when run
        with `--close` on the person's request in words, and carries no such
        flag, so an agent closing its own tab never finishes a card it did
        not close itself.
        """
        if session_id in self._prompts_by_session():
            # `close_terminal` sends no keystrokes, so `send_text`'s newline
            # argument no longer applies. It stays because a session holding
            # an open permission prompt is mid-turn asking to act: closing
            # the tab SIGHUPs it inside a tool call, the prompt row would be
            # orphaned, and this button's whole meaning is "I have finished
            # reading". `_wrap_up_for_done` already made this exact call for
            # the board's close leg.
            return False, PROMPT_BLOCKED_REFUSAL
        record = self._codex_records.get(session_id)
        identity = record.process_identity if record is not None else None
        if (by_person is True and record is not None
                and (identity is None or identity.match_kind != "explicit_resume")):
            ok, detail = await self._close_native_codex_by_person(session_id)
        else:
            ok, detail = await self._close_session_terminal(session_id)
        if ok:
            if by_person:
                # After the close, never before: the tab is gone, so a card
                # moved first would be a card finished by a close that then
                # refused. Best-effort and silent — it never raises into the
                # close and never changes the close's answer.
                await self._finish_card_for_closed_session(session_id)
                # And whatever the card or the agent still had on Needs
                # you goes with it, as Dismiss would take it.
                await self._settle_closed_session(session_id)
            return True, ""
        if detail == "no pid":
            return False, "Dark Army has no process on record for this session."
        if detail == "no 0.1.9+ window matched":
            return False, NO_CLOSING_WINDOW_REFUSAL
        if detail == "codex identity changed":
            return False, CODEX_IDENTITY_REFUSAL
        return False, detail

    def _carded_live_sessions(self) -> set:
        """The sessions Dark Army itself started for a card, while that link is live.

        One function, so "this session is working a card" is decided once:
        `message_card` re-checks the card at the press and the enriched row
        publishes the reach, and a second walk over `link_state` would let the
        two disagree about which sessions may be typed at.

        Read off `self._board_state`, the last *published* board, which
        `_build_board_state` replaces whole rather than mutating — so this is
        safe from the executor with no lock. It is one frame stale by
        construction (`_enrich_agent_stubs` runs before `_reconcile_board`),
        and stale in the safe direction: a just-bound card reads false for one
        frame and then true, never the other way round.
        """
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        out = set()
        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("link_state") != "live":
                continue
            sid = str(card.get("session_id") or "")
            if sid:
                out.add(sid)
        return out

    def _card_field_by_session(self, field: str) -> dict[str, str]:
        """`session_id -> card[field]` over the last published board — the
        two-pass walk `_card_titles_by_session` states, factored so the push
        can name a session's card *id* by the same rule it names the title.

        Same passes, same first-wins order, and a card whose `field` is empty
        after whitespace-collapsing is skipped exactly as an untitled card
        was — so `_card_titles_by_session`'s answers are byte-identical
        (`test_push_subject.py` pins them).
        """
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        out: dict[str, str] = {}
        for active_only in (True, False):
            for card in cards:
                if not isinstance(card, dict):
                    continue
                value = " ".join(str(card.get(field) or "").split())
                if not value:
                    continue
                for sid_key, state_key in (
                    ("session_id", "link_state"),
                    ("refine_session_id", "refine_state"),
                ):
                    if active_only and card.get(state_key) not in _ACTIVE_CARD_LINKS:
                        continue
                    sid = str(card.get(sid_key) or "")
                    if sid and sid not in out:
                        out[sid] = value
        return out

    def _card_titles_by_session(self) -> dict:
        """Which card names each session, for a row that has no name of its own.

        Both links count: `session_id` (Start) and `refine_session_id`
        (Refine). Read off `self._board_state`, the last *published* board,
        which `_build_board_state` replaces whole rather than mutating — so
        this is safe from the executor with no lock, exactly as
        `_carded_live_sessions` states. No store read: `BoardStore.by_session`
        would be a SQLite query per row per refresh.

        Two passes, because `update()` does not clear `session_id` when a card
        leaves In progress: pass one records only the *active* links, pass two
        fills the gaps from any card naming that session. Within a pass the
        first card in snapshot order wins, so the result is deterministic.
        """
        return self._card_field_by_session("title")

    def _card_ids_by_session(self) -> dict[str, str]:
        """`session_id -> card id`, by `_card_titles_by_session`'s rule — the
        card the buzz's subject names (`_compose_push_subject`)."""
        return self._card_field_by_session("id")

    def _card_title_by_id(self) -> dict[str, str]:
        """`card_id -> title`, for the origin stamp to say what it was for.

        `_card_titles_by_session`'s sibling and deliberately not a widening of
        it: that function answers "which card names this session", published
        only beside `UNNAMED_SESSION`, and its presence *is* that judgment.
        This one answers "what is this card called", asked of a stamp that
        named the card before any session had bound to it — which is exactly
        the frame a person is reading when they ask "who is this?".

        Read off `self._board_state`, the last *published* board, which
        `_build_board_state` replaces whole rather than mutating — so this is
        safe from the executor with no lock. It is one frame stale by
        construction (`_enrich_agent_stubs` runs before `_reconcile_board`),
        and stale in the harmless direction: a title edited this second is
        drawn one push later.
        """
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        out: dict[str, str] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = str(card.get("id") or "")
            title = " ".join(str(card.get("title") or "").split())
            if cid and title and cid not in out:
                out[cid] = title
        return out

    def _card_area_by_id(self) -> dict[str, str]:
        """Area metadata from the last published board, like card titles."""
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        return {str(c["id"]): str(c.get("area") or "") for c in cards
                if isinstance(c, dict) and c.get("id")}

    def _card_roles_by_session(self) -> dict[str, tuple[str, str, str]]:
        """Active card links as (start/refine, card id, area), first card wins."""
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        out: dict[str, tuple[str, str, str]] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = str(card.get("id") or "")
            for sid_key, state_key, kind in (
                ("refine_session_id", "refine_state", "refine"),
                ("session_id", "link_state", "start"),
            ):
                if card.get(state_key) not in _ACTIVE_CARD_LINKS:
                    continue
                sid = str(card.get(sid_key) or "")
                if sid and sid not in out:
                    out[sid] = (kind, cid, str(card.get("area") or ""))
        return out

    def _card_role_from_origin(self, sid: str,
                               stub: dict) -> tuple[str, str, str]:
        """The role a not-yet-bound session was *started* for, from its stamp.

        `_card_roles_by_session` reads the published board, which learns the
        session only at the bind — a frame after `_assign_nicknames` has
        already named it. Without this, every Start wore a hashed stranger for
        a frame and was then renamed to its area lead (`_rename_for_role`),
        so the face changed the moment somebody pressed Start. The origin
        stamp rides the session from its very first hook, so the first pick
        can already be the role's character. A child row a parent claims is
        never the session a Start opened, and neither is a non-interactive one.
        """
        if self._parent_of_child(sid):
            return ("", "", "")
        if (stub.get("kind") or "interactive") != "interactive":
            return ("", "", "")
        stamped = origin.parse(
            (self._session_states.get(sid) or {}).get("origin")
            or stub.get("origin") or "")
        if not stamped:
            return ("", "", "")
        kind = {"card-start": "start",
                "card-refine": "refine"}.get(stamped.get("by") or "", "")
        card_id = stamped.get("card_id") or ""
        if not kind or not card_id:
            return ("", "", "")
        return (kind, card_id, self._card_area_by_id().get(card_id, ""))

    def _role_nickname(self, kind: str, card_id: str, area: str,
                       taken: Iterable[str] = ()) -> str:
        """Chief of staff for Refine, area's free lead for Start; no duplicates.

        ART_ONLY never names a session. An exhausted pool returns no preference,
        leaving IdentityStore to choose a free ordinary cast name.
        """
        busy = {n.split("-")[0].lower() for n in taken}
        busy |= {s.lower() for s in identity.ART_ONLY}
        if kind == "refine":
            who = areas.CHIEF_OF_STAFF
        elif kind == "start":
            # The face the card showed when Start was pressed, if it is still
            # free (`_promised_lead`); else today's first free pool member.
            promised = (getattr(self, "_lead_promises", {}) or {}).get(card_id, "")
            who = (promised if promised and promised not in busy
                   else areas.allocate(area, card_id, busy))
        else:
            return ""
        if not who or who in busy:
            return ""
        return next((n for n in identity.NAMES if n.lower() == who), "")

    def _low_priority_recent(self, session_id: str) -> bool:
        """Whether Dark Army typed `/low-priority` at this session within the
        cooldown. The one predicate both the verb and the reach flag call.
        A pure read: the flag calls it from the executor, so pruning here
        would race the verb's write on the loop (an executor `pop` of a
        just-expired stamp could land after the loop wrote a fresh one,
        silently dropping the toggle guard). Expired entries are pruned
        on the loop, in `_prune_low_priority_sent`, before the verb writes."""
        at = self._low_priority_sent.get(session_id)
        return at is not None and (
            time.monotonic() - at <= LOW_PRIORITY_COOLDOWN_SECONDS)

    def _prune_low_priority_sent(self) -> None:
        """Drop expired cooldown stamps. Loop-only, so it never races the
        executor's read in `_low_priority_recent`."""
        now = time.monotonic()
        for sid, at in list(self._low_priority_sent.items()):
            if now - at > LOW_PRIORITY_COOLDOWN_SECONDS:
                self._low_priority_sent.pop(sid, None)

    async def low_priority_session(self, session_id: str) -> tuple[bool, str]:
        """Carry a rate-limited Claude session on in low-priority mode.

        `wrap_up_session`'s sibling: `/low-priority` is typed onto the
        session's own input line through VS Code's `sendText` — a slash
        command is expanded by the client, so the channel cannot carry it —
        and only once that has landed is the card dropped. Four refusals, in
        this order: an open permission prompt (`sendText` ends in a newline a
        dialog reads as confirm), a provider that is not Claude (an unstamped
        session refuses too — it has no proven pid either), a current card
        that is not a rate-limit `StopFailure`, and a success within
        `LOW_PRIORITY_COOLDOWN_SECONDS` (the command is a toggle).

        The state write is the unusual half. `dismiss_notification` alone
        leaves `state == "error"`, which `categorize` still banks under
        `waiting`, so the card would go and the row would stay in Needs you.
        Writing `idle` and re-stamping the clocks, the way the `Stop` branch
        of `_update_session_state` does, reads `sleeping` until the session's
        next hook event overrides it as normal. Never `thinking` — that is
        `UserPromptSubmit`'s word.
        """
        if session_id in self._prompts_by_session():
            return False, PROMPT_BLOCKED_REFUSAL
        if self._session_provider(session_id) != "claude":
            return False, LOW_PRIORITY_NOT_CLAUDE_REFUSAL
        card = self._active_notifications.get(session_id) or {}
        if not (card.get("hook") == "StopFailure"
                and card.get("error_kind") == "rate_limit"):
            return False, LOW_PRIORITY_NOT_LIMITED_REFUSAL
        if self._low_priority_recent(session_id):
            return False, LOW_PRIORITY_ALREADY_REFUSAL

        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if not pid:
            return False, "No PID on record for this session."

        result = await session_io.send_text(
            pid, "", INPUT_LINE_CLEAR + LOW_PRIORITY_COMMAND)
        if not (result and result.get("sent")):
            return False, _typed_nothing_refusal(result) or NO_TYPING_WINDOW_REFUSAL

        self._prune_low_priority_sent()
        self._low_priority_sent[session_id] = time.monotonic()
        st = self._session_states.get(session_id)
        if st is not None:
            st["state"] = "idle"
            st["last_event"] = time.time()
            st["last_event_monotonic"] = time.monotonic()
        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Low priority on %s via %s", session_id[:12], where)
        await self.dismiss_notification(session_id)
        return True, ""

    async def wrap_up_or_close_session(self, session_id: str) -> tuple[bool, str]:
        """The `wrap_up` API action's new meaning: close first, clear as fallback.

        Only stale `close-out.sh` copies still send `wrap_up`. Grok is
        refused here before any close: hosted Grok close succeeds mid-tool
        and vanishes the row this ship is meant to keep. With
        `board_close_terminal_enabled` on, the tab is closed instead — the
        current script sends `close_terminal`, and only under `--close` on
        the person's request, never `wrap_up` — and `/clear` is typed only
        when the close is refused for a reason other than an open
        permission prompt or a Codex identity mismatch. Those two are
        refused in `close_session_terminal`'s own words and never cleared.
        `_wrap_up_for_done` does not come through here; it has no
        wrap-up fallback.
        """
        if (self._session_provider(session_id) == "grok"
                or session_id in self._grok_records):
            return False, GROK_WRAP_UP_REFUSAL
        if not self.board_close_terminal_enabled:
            return await self.wrap_up_session(session_id)
        ok, detail = await self.close_session_terminal(session_id)
        if ok:
            logger.info("wrap_up from an old close-out script for %s: "
                        "closed the terminal instead of clearing",
                        session_id[:12])
            return True, ""
        if detail in (PROMPT_BLOCKED_REFUSAL, CODEX_IDENTITY_REFUSAL):
            return False, detail
        logger.info("wrap_up for %s: close refused (%s); typing /clear",
                    session_id[:12], detail)
        return await self.wrap_up_session(session_id)

    async def _close_session_terminal(self, session_id: str) -> tuple[bool, str]:
        """Dispose the VS Code terminal tab owning this session. `(ok, detail)`.

        `wrap_up_session`'s sibling, not a modification of it: same pid ladder,
        same act-first-bookkeep-second order, but the act is
        `session_io.close_terminal` — the tab and the process both go (VS
        Code SIGHUPs the process group) instead of `/clear` being typed. Three
        callers: `close_session_terminal` (the panel button); `_wrap_up_for_done`,
        behind `board_close_terminal_enabled`, where every failure is a
        *fallback* onto `wrap_up_session`; and `_close_for_deleted_card`, which
        is unconditional — no preference, no fallback. The last two
        short-circuit Codex above this function, so the third pid rung below
        does not change their behaviour. The detail is for the log only and
        is never minted into a card-visible string.

        The bookkeeping on success is the wrap-up's Grok third act, for **both**
        providers: disposing the terminal makes neither Claude nor Grok
        reliably emit SessionEnd (the process is SIGHUP'd), and without the
        forget the row lingers until `_check_liveness`'s next tick. The reason
        is `"closed"`, which is in `GROK_END_REASONS` because the tab is gone —
        exactly that set's admission test. A racing SessionEnd, if the CLI
        manages one, double-forgets, which is safe: `_forget_session` is
        pop-with-default throughout. A Codex sid re-opens the live process
        through `matching_process_identity` (Stop's proof) and settles via
        `_confirm_codex_stop` after the pid is gone — hide is revision-keyed,
        so stamping the pre-death journal would restore the row on the next
        flush. `_forget_session` does not know about Codex.
        """
        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        # A terminal Dark Army itself owns is resolved by **session name**, not by
        # pid: once the child has exited `ptyhost.owns(pid)` is None (its
        # pid may be somebody else's by now) while Dark Army still holds the
        # master for `EXITED_RETENTION_SECONDS`. `session_io` would fall to
        # VS Code and refuse in the wrong words.
        pty_handle = self._pty.for_session(session_id)
        codex_identity = None
        if pid is None and pty_handle is None:
            # Third rung, Codex only: a uniquely owned native
            # `codex resume <thread>` CLI. `unique_cwd` and child rows have
            # a pid Dark Army must not aim a close at. The snapshot pid is not
            # enough — Stop re-opens exe/cwd/create_time/argv at fire time
            # and so does this, or a recycled pid would dispose the wrong tab.
            record = self._codex_records.get(session_id)
            identity = record.process_identity if record is not None else None
            if (record is not None
                    and identity is not None
                    and identity.match_kind == "explicit_resume"
                    and not record.is_child):
                proc = codex_rollouts.matching_process_identity(
                    record,
                    identity,
                    destructive=True,
                    records=list(self._codex_records.values()),
                )
                if proc is None:
                    if not psutil.pid_exists(identity.pid):
                        await self.dismiss_notification(session_id)
                        self._note_closed(
                            session_id,
                            pid=identity.pid,
                            create_time=identity.create_time,
                        )
                        self._settle_codex_stop(session_id, "closed")
                        return True, ""
                    return False, "codex identity changed"
                pid = proc.pid
                codex_identity = identity
        is_grok = (
            (st is not None and st.get("provider") == "grok")
            or session_id in self._grok_records
        )
        if pid and is_grok and not _grok_session_pid_ok(pid):
            # Never aim `close_terminal` at the shared `grok agent leader`.
            roster = self._roster_pid(session_id)
            pid = roster if roster and _grok_session_pid_ok(roster) else None
        if not pid and pty_handle is None:
            return False, "no pid"

        if pty_handle is not None:
            result = await ptyhost.close(pty_handle)
        else:
            result = await session_io.close_terminal(pid, "")
        if not (result and result.get("matched")):
            return False, "no 0.1.9+ window matched"

        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Closed the terminal of %s via %s", session_id[:12], where)
        await self.dismiss_notification(session_id)
        if codex_identity is not None:
            self._note_closed(
                session_id,
                pid=codex_identity.pid,
                create_time=codex_identity.create_time,
            )
        else:
            self._note_closed(session_id)
        if codex_identity is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                asyncio.ensure_future(
                    self._confirm_codex_stop(
                        session_id, codex_identity, reason="closed"))
        else:
            roster_only = (
                session_id not in self._session_states
                and session_id in self._agent_records
            )
            rec = self._agent_records.get(session_id) if roster_only else None
            self._forget_session(session_id, "closed")
            if rec is not None and session_id not in self._finished:
                self._record_finished(
                    session_id,
                    {"project": rec.project, "pid": rec.pid,
                     "state": rec.activity},
                    "closed",
                )
            # `_forget_session` does not drop the Claude roster. Leave it in
            # place and the next snapshot re-lists this id as a live stub
            # until the poll rebuilds — `_forget_stopped`'s own reason for
            # popping here rather than waiting.
            self._agent_records.pop(session_id, None)
            self._blocked_since.pop(session_id, None)
            self._agents_poller.poll_soon()
            self._persist_sessions()
            await self._wake_surfaces()
        return True, ""

    async def _close_terminal_by_pid(self, pid: int) -> tuple[bool, str]:
        """Dispose the VS Code terminal tab owning this pid. `(ok, detail)`.

        `_close_session_terminal`'s sibling for a still-dispatching card:
        the pid is the terminal process Dark Army itself opened, not a grok/claude
        process, so this does not run `_grok_session_pid_ok` and does not
        `_forget_session` — nothing is bound yet.
        """
        if not pid or pid <= 1:
            return False, "no pid"
        result = await session_io.close_terminal(pid, "")
        if not (result and result.get("matched")):
            return False, "no 0.1.9+ window matched"
        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Closed the terminal of pid %s via %s", pid, where)
        return True, ""

    async def answer_question(self, session_id: str, question_id: str,
                              option_index: int) -> tuple[bool, str]:
        """Choose an option of the `AskUserQuestion` this session is stopped on.

        The channel cannot carry this — a pushed event queues *behind* the very
        dialog it would answer — but the keystroke route lands in front of it,
        exactly as `/clear` and `/compact` do: `session_io.send_text` into
        the session's own VS Code terminal. The dialog is a keyboard select,
        read out of the installed client rather than assumed: a **digit
        selects the option at that absolute index and advances** (Claude
        Code 2.1.261; see `PICK_ONE_SUBMIT_KEYS`). Enter selects the
        *focused* option. Dark Army types the digit alone. The writes are
        separate on purpose — see `QUESTION_KEY_GAP_SECONDS`.

        Deliberately touches nothing on success: no card is dismissed and
        `_questions` is left alone, because the question clears itself the
        moment the transcript carries its `tool_result` — which is the only
        proof the choice actually landed.
        """
        # One burst per session at a time — the batch verb's own rule, and it
        # has to hold across both verbs or it holds for neither: a singular
        # press landing while `answer_questions`' ~1s of keystrokes is still
        # in flight would interleave a digit and an Enter into the middle of
        # somebody else's answer on the same input line.
        if session_id in self._answering:
            return False, ("Already typing an answer into that session — "
                           "wait for it to finish.")
        if session_id in self._prompts_by_session():
            # Same trap `wrap_up_session` and `_flush_auto_compacts` document,
            # and worse here: a relayed tool-approval dialog owns the input
            # line, and our Enter would confirm the *permission* dialog's
            # highlighted choice instead of ours.
            return False, ("That session is waiting on a permission prompt — "
                           "answer it first.")

        # And the state at the instant of the act, which is the house rule for
        # every verb here that reaches into a session: `stop_session` re-checks
        # process identity, `delete_abandoned_agent` re-reads the category,
        # `dispatch_card` re-reads the card out of `board.db`. This route is in
        # the same family and had no such check — it is a *keystroke*, not a
        # message, so a question whose dialog has closed sends a digit onto an
        # idle input line, where it is submitted as an ordinary prompt and the
        # agent reads a bare "2" as an instruction from
        # the human. The id check does not catch that: the panel sends back the
        # very id being held, so it matches.
        #
        # Holding a question the dialog no longer owns is not hypothetical
        # since `_restore_pending_questions` — the hook that would have said
        # "answered" is dropped while the daemon is down (`dark-army-notify`
        # exits 0 with nothing listening), so a restart can bring back a
        # question the human dealt with seconds earlier. The slot self-corrects
        # on that session's next hook event, which makes the window short
        # rather than absent, and short is not a property worth betting
        # somebody's session on.
        #
        # `waiting` is the right test for a hook-tracked session: the
        # `tool_use` branch sets it for `AskUserQuestion` and the
        # `PermissionRequest` companion re-asserts it. Grok also flushes the
        # asking turn to chat_history while the dialog is up, so a row the
        # hooks have not stamped can still be blocked — `_questions` is that
        # live reading, and a matching tool_result clears it. A session with
        # no state *and* no live Grok question is refused for the reason the
        # `permission` branch never creates one — a late verb must not act
        # on a session that has ended.
        if not self._session_waiting_on_question(session_id):
            return False, "That session is no longer waiting on a question."

        q = self._questions.get(session_id)
        if not q:
            return False, "That session is not waiting on a question."
        # A dialog of several questions advances on each answer, so answering
        # just one leaves the rest standing on the terminal with nothing
        # saying so — the half-answer is refused in words instead. Only an
        # out-of-date surface hits this: a current panel or phone sends the
        # batch verb.
        held_count = len(q.get("questions") or [])
        if held_count > 1:
            return False, (f"This dialog asks {held_count} questions — answer "
                           "them together (update Dark Army's panel or phone if you "
                           "see no way to).")
        # Matched on the tool_use id, so a verdict aimed at a question the
        # terminal already dealt with misses rather than landing on its
        # successor — the argument `answer_permission` makes for `request_id`.
        # Empty on either side is tolerated: a pre-upgrade panel, or a
        # transcript that carried no id, must not be locked out.
        held_id = str(q.get("id") or "")
        if question_id and held_id and question_id != held_id:
            return False, "That question has already been answered."
        options = q.get("options") or []
        # `< 9` because the digit keys are the whole route: only options 1–9
        # are reachable. A guard rather than a limit — MAX_OPTIONS is 4.
        if not (0 <= option_index < len(options)) or option_index >= 9:
            return False, "No such option."

        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if not pid:
            return False, "No PID on record for this session."

        # One-question dialogs — pick-one or multi-select — share the burst
        # so the two verbs cannot drift. `_type_answer_burst` holds
        # `_answering`, applies `PICK_ONE_SUBMIT_KEYS` /
        # `MULTI_SELECT_SUBMIT_KEYS` by the question's own flags, and sends
        # the trailing Enter only where the client shows a summary screen
        # (`_dialog_has_summary`: never for a single pick-one question).
        listed = [item for item in (q.get("questions") or [])
                  if isinstance(item, dict)]
        q_first = listed[0] if listed else q
        return await self._type_answer_burst(
            session_id, pid, [q_first], [[option_index]])

    async def answer_questions(self, session_id: str, question_id: str,
                               option_indexes: list) -> tuple[bool, str]:
        """Answer every question of the dialog this session is stopped on.

        `answer_question`'s sibling for the dialogs that ask more than once:
        the tool takes up to four questions per call, the terminal shows them
        one at a time and advances on each answer, and digits address the
        question *currently shown* — so an answer aimed at question 3 alone
        is meaningless unless 1 and 2 go down first, in order, in the same
        burst. One ordered burst, `QUESTION_KEY_GAP_SECONDS` between every
        consecutive keystroke: a pick-one question is its digit, a
        `multi_select` question is one digit per pick then
        `MULTI_SELECT_SUBMIT_KEYS`, a `has_preview` pick-one question is its
        digit then `PREVIEW_PICK_ONE_SUBMIT_KEYS`, and a dialog that ends on
        the summary screen (`_dialog_has_summary`) ends with the Enter that
        accepts it. Bounded at
        MAX_QUESTIONS × (MAX_OPTIONS + len(MULTI_SELECT_SUBMIT_KEYS)) + 1
        sends (37, ~4.4s) — a multi-select question costs up to nine sends
        where a pick-one costs one. The typing itself is `_type_answer_burst`.

        `option_indexes` is one group per question, in dialog order: a
        `list`/`tuple` of 0-based indexes, or a bare `int` (in-process
        callers) taken as a group of one. The daemon never trusts the
        client's list beyond the choices themselves: the dialog is re-read
        out of `self._questions` at the instant of the act, the length must
        match what *it* holds, every index is validated against its own
        question's options, and a group's *shape* is judged against that
        question's `multi_select` — several picks on a pick-one question, an
        empty group, or the same option twice (a repeated digit would toggle
        the box off again) are refused in words — all before the first
        keystroke. Dark Army types blind, so a mid-burst send failure stops
        immediately and says exactly how far it got; a partial outcome is
        loud, never silent. No retries — a retried digit lands on whatever
        dialog is up next.
        """
        # One burst per session at a time. The guards below are re-checks of
        # *the session's* state; this one is a re-check of *ours* — a second
        # press while ~1s of keystrokes is still in flight would interleave
        # two answers on one input line.
        if session_id in self._answering:
            return False, ("Already typing an answer into that session — "
                           "wait for it to finish.")
        if session_id in self._prompts_by_session():
            # Same trap `answer_question` documents: a relayed tool-approval
            # dialog owns the input line, and our Enter would confirm *its*
            # highlighted choice instead of ours.
            return False, ("That session is waiting on a permission prompt — "
                           "answer it first.")
        # The state at the instant of the act — `answer_question`'s argument,
        # made longer: this burst is up to 37 keystrokes, and every one of
        # them lands as an ordinary prompt if the dialog has closed.
        if not self._session_waiting_on_question(session_id):
            return False, "That session is no longer waiting on a question."

        q = self._questions.get(session_id)
        if not q:
            return False, "That session is not waiting on a question."
        held_id = str(q.get("id") or "")
        if question_id and held_id and question_id != held_id:
            return False, "That question has already been answered."
        questions = [item for item in (q.get("questions") or [])
                     if isinstance(item, dict)]
        if not questions:
            # An older reading holds only the flat question; a one-answer
            # burst on it is exactly the singular verb's act.
            questions = [q]
        # Each element normalised to a group: an int (an in-process caller)
        # is a group of one, a list/tuple is taken as the group, anything
        # else — a str, a dict — is not a choice.
        groups: list[list[int]] = []
        try:
            for element in option_indexes:
                if isinstance(element, bool):
                    return False, "No such option."
                if isinstance(element, int):
                    groups.append([element])
                elif isinstance(element, (list, tuple)):
                    groups.append([int(index) for index in element])
                else:
                    return False, "No such option."
        except (TypeError, ValueError):
            return False, "No such option."
        # The length is matched against what *we* hold, never the client's
        # claim: a short burst half-answers the dialog, a long one types the
        # surplus onto whatever the terminal shows next.
        if len(groups) != len(questions):
            return False, (f"This dialog asks {len(questions)} questions — "
                           f"it needs {len(questions)} answers, one per "
                           f"question, and got {len(groups)}.")
        # Every group against its own question, before the first keystroke —
        # a burst that would fail on question 3 must not answer questions 1
        # and 2 first. `< 9` because the digit keys are the whole route,
        # exactly as in `answer_question`. A duplicate is refused rather than
        # deduped: on the multi-select widget a repeated digit toggles the
        # box off again, and a silent dedupe would hide a client bug. Several
        # picks on a pick-one question are refused because that widget has
        # no way to take them — a second digit would answer the next question.
        for position, (group, item) in enumerate(zip(groups, questions)):
            options = item.get("options") or []
            if not group:
                return False, f"No such option for question {position + 1}."
            for choice in group:
                if not (0 <= choice < len(options)) or choice >= 9:
                    return False, (f"No such option for question "
                                   f"{position + 1}.")
            if len(set(group)) != len(group):
                return False, f"Question {position + 1} names the same option twice."
            if len(group) > 1 and item.get("multi_select") is not True:
                return False, f"Question {position + 1} takes one answer."

        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if not pid:
            return False, "No PID on record for this session."

        return await self._type_answer_burst(session_id, pid, questions, groups)

    async def _type_answer_burst(self, session_id: str, pid: int,
                                 questions: list, groups: list
                                 ) -> tuple[bool, str]:
        """Type one validated answer burst into the session's terminal.

        The typing half of `answer_questions`, shared with `answer_question`
        for a one-question dialog. `questions` and `groups` are already
        validated, one group per question in dialog order. Per question: a
        pick-one question sends its digit alone — the digit selects and
        advances on the installed client (see `PICK_ONE_SUBMIT_KEYS`); a
        `multi_select` question sends one digit per pick in ascending order,
        then every element of `MULTI_SELECT_SUBMIT_KEYS`; a `has_preview`
        pick-one question sends its digit then `PREVIEW_PICK_ONE_SUBMIT_KEYS`.
        The burst ends with one Enter where the client shows its summary
        screen (`_dialog_has_summary`).
        `QUESTION_KEY_GAP_SECONDS` between every consecutive send. Holds
        `self._answering` across the whole burst and releases it on every
        exit.
        """
        capture = getattr(self, "_decisions", None)
        episode_id = capture.session_items.get(session_id) if capture else None
        self._answering.add(session_id)
        try:
            # `reached` counts the questions whose first digit landed: on a
            # later failure inside that question the terminal is on it and
            # the count says so; on a first-digit failure the previous
            # question was the last one touched. Either way the sentence
            # names where the terminal is. On a multi-select question a
            # mid-sequence failure leaves boxes toggled and nothing
            # submitted — the same sentence sends the human to the terminal.
            reached = 0
            sent_any = False
            result = None
            for group, item in zip(groups, questions):
                if item.get("multi_select") is True:
                    sequence = ([str(choice + 1) for choice in sorted(group)]
                                + list(MULTI_SELECT_SUBMIT_KEYS))
                elif item.get("has_preview") is True:
                    # The preview layout: the digit only focuses, Enter
                    # selects (see `PREVIEW_PICK_ONE_SUBMIT_KEYS`).
                    sequence = ([str(group[0] + 1)]
                                + list(PREVIEW_PICK_ONE_SUBMIT_KEYS))
                else:
                    sequence = ([str(group[0] + 1)]
                                + list(PICK_ONE_SUBMIT_KEYS))
                for offset, keystroke in enumerate(sequence):
                    if sent_any:
                        await asyncio.sleep(QUESTION_KEY_GAP_SECONDS)
                    result = await session_io.send_text(
                        pid, "", keystroke, newline=False)
                    if not (result and result.get("sent")):
                        if not sent_any:
                            return False, _typed_nothing_refusal(result) or NO_TYPING_WINDOW_REFUSAL
                        return False, (f"Answered {reached} of "
                                       f"{len(questions)} — the window "
                                       "stopped answering; finish the rest "
                                       "in the terminal.")
                    sent_any = True
                    if offset == 0:
                        reached += 1
            # A dialog that ends on the summary screen does not end on the
            # last answer: the terminal shows "Review your answers" and waits
            # to be confirmed, with "Submit answers" already focused. Without
            # this Enter the burst leaves that screen standing, the session
            # stays `waiting` on the very dialog it just filled in, and every
            # surface draws the interview again as if nothing had been
            # answered. One more Enter, after the same gap as every other
            # keystroke, and it is the last thing typed.
            #
            # Gated on the client's own rule (`_dialog_has_summary`) rather
            # than on the count: a one-question *pick-one* dialog has no such
            # screen — its digit submits — and a spare Enter there would land
            # on an idle input line; a one-question *multi-select* dialog
            # does have it, and used to be left standing on it.
            if _dialog_has_summary(questions):
                await asyncio.sleep(QUESTION_KEY_GAP_SECONDS)
                result = await session_io.send_text(
                    pid, "", "\r", newline=False)
                if not (result and result.get("sent")):
                    return False, (f"All {len(questions)} answers were "
                                   "entered, but the confirmation was not "
                                   "sent — confirm it in the terminal.")
        finally:
            self._answering.discard(session_id)

        capture = getattr(self, "_decisions", None)
        if capture is not None:
            capture.submit("finish", "question:" + session_id,
                           "delivered_unconfirmed",
                           "Answer choices sent: " + str(groups),
                           "keystrokes delivered; execution not confirmed",
                           episode_id=episode_id or "missing")
        where = result.get("terminalName") or result.get("matchedBy") or "terminal"
        logger.info("Answered %d question(s) on %s via %s",
                    len(questions), session_id[:12], where)
        return True, ""

    def _decide_permission_ask(self, msg: dict) -> dict:
        """Whether to hold this session's permission dialog open for Dark Army.

        The reply is the whole of the contract with the installed hook script:
        `{"hold": 0}` means "carry on without me" and the script exits having
        printed nothing, leaving the terminal's own dialog exactly as it is
        today. A positive hold means Dark Army has a row somebody can answer.

        The refusals, and each of them is a bug avoided rather than a
        policy:

        * **An ask raised inside a subagent.** The whole safety case for
          holding a dialog open is that the CLI runs its hooks *concurrently*
          with the dialog — but the async-subagent spawn path sets
          `awaitAutomatedChecksBeforeDialog`, which awaits the hooks to
          completion **before** the dialog is presented. A hold there is not a
          second way to answer a question; it is a silent freeze with nothing
          on screen to answer. So a `session_id` that resolves to a child of a
          session Dark Army knows is refused outright, before the parent redirect
          can make it look like an ordinary ask.
        * **AskUserQuestion.** `PermissionRequest` fires as its companion, and
          that dialog already has its own answer path. Brokering it would put
          two surfaces on one question.
        * **A live channel.** The relay already covers that session, with a
          request id of its own; a second row for one dialog is two cards,
          two buzzes and two ways to answer half of it.
        * **A session Dark Army has no state for.** A row nobody can render, on a
          session that never announced itself — the same argument the legacy
          `permission` event's `create=False` makes.
        * **A request id already open.** Overwriting the row would replace its
          claim, so the script that is actually polling could never collect a
          verdict again — a permanently unanswerable prompt on every surface.

        Past the refusals, minting the new row **pops every earlier hook row
        for the same session**. A session can only be stopped on one dialog at
        a time, so this ask is itself the proof that the previous one is over —
        no clock, no state read, no snapshot pass. It is the rung that closes
        the *same-turn shadow*, which the two clock/state rungs in
        `_reap_permissions` structurally cannot: between two asks in one turn
        the session sits in `waiting` for the whole interval (the first ask's
        own `permission` event stamps it, with a `last_event` a hair later than
        `asked_at`), and the only `working` moment is the sub-100ms gap between
        `PreToolUse` and `PermissionRequest`, which a read-driven reap misses.
        Without this the spent row would be the one `_prompts_by_session`
        draws, so every surface would name the finished tool, `alerts` would
        key "already buzzed" on the spent request id and raise nothing for the
        live ask, and reply/typing/wrap-up/`can_close` would stay refused
        behind a dead row for the whole of the hold.

        It sits **after** the refusals on purpose: a refused ask evicts
        nothing, so a subagent's ask, an `AskUserQuestion` companion or a
        duplicate request id can never take a live row down with it. Channel
        rows are never touched — the channel serialises its own prompts and
        has a request id of its own.

        Returns the reply; the caller runs the ordinary `permission` path
        either way, so the session still goes to `waiting` on a refusal.
        """
        raw_sid = str(msg.get("session_id") or "")
        child_of = self._parent_of_child(raw_sid)
        provider = str(msg.get("provider") or "claude")
        codex_record = (codex_rollouts.record_for_hook_session(
            self._codex_records, raw_sid) if provider == "codex" else None)
        sid = child_of or (codex_record.session_id if codex_record else raw_sid)
        request_id = str(msg.get("request_id") or "")
        tool_name = str(msg.get("tool_name") or "")
        claim = str(msg.get("claim") or "")
        why = ""
        if not request_id:
            why = "no request id"
        elif child_of or (provider == "codex" and msg.get("agent_type")):
            why = "a subagent's dialog is not answerable from here"
        elif request_id in self._permission_requests:
            why = "that request id is already open"
        elif not claim:
            # Fail closed, `channel_server.listen()`'s discipline: the claim is
            # the only thing that makes a verdict collectable by the script
            # that asked, and `hmac.compare_digest("", "")` is True — a row
            # minted with no claim would publish its request id in the ungated
            # /api/state and let any local process take the answer.
            why = "no claim token"
        elif is_ask_user_question(tool_name):
            why = "the question flow owns that dialog"
        elif provider == "codex" and codex_record is None:
            why = "no unique Codex root to show it on"
        elif not sid or (sid not in self._session_states and codex_record is None):
            why = "no session to show it on"
        elif self._channel_for_session(sid) is not None:
            why = "its channel already relays permissions"
        if why:
            if child_of and child_of in self._session_states:
                self._session_states[child_of]["ask_note"] = (
                    "Raised inside a helper agent — answer it in the terminal.")
            logger.info("Not brokering %s for %s: %s",
                        tool_name or "?", sid[:12] or "?", why)
            return {"hold": 0}
        asked_at = time.time()
        # The same-turn shadow, closed by evidence rather than by a clock: the
        # session cannot be stopped on two dialogs at once, so this ask ends
        # every earlier hook row it has. `verdict` is checked the way
        # `_reap_permissions` checks it, so a row already carrying a staged
        # answer does not write a false "lapsed" diary line.
        spent_why = "the session asked about something else"
        for spent_rid in [rid for rid, other
                          in self._permission_requests.items()
                          if other.get("via") in ("hook", "codex-hook")
                          and other.get("session_id") == sid]:
            spent = self._permission_requests.pop(spent_rid, None)
            if spent is None:
                continue
            logger.info("Dropping permission prompt %s: %s",
                        spent_rid, spent_why)
            if not spent.get("verdict"):
                self._log_permission(spent, "permission_resolved",
                                     outcome="lapsed", why=spent_why)
        self._permission_requests[request_id] = {
            "request_id": request_id,
            "session_id": sid,
            "tool_name": tool_name,
            # Untrusted all the way to whatever renders them, and clamped
            # here as well as in the script: that copy is on disk and may be
            # older than this process.
            "description": clip(str(msg.get("description") or ""),
                                MAX_PROMPT_DESCRIPTION_CHARS),
            "input_preview": clip(str(msg.get("input_preview") or ""),
                                  MAX_PROMPT_INPUT_CHARS),
            "asked_at": asked_at,
            # No `port` key: this row has no channel behind it, which is
            # exactly what `_reap_permissions` and `_permission_snapshot`
            # branch on.
            "via": "codex-hook" if provider == "codex" else "hook",
            "provider": provider,
            # A secret with the channel secret's discipline: it is what makes
            # the verdict collectable by the script that asked and nothing
            # else on this machine. Never published, never persisted.
            **({"claim": claim, "hold_until": asked_at + HOOK_PROMPT_HOLD_SECONDS,
                "last_poll_at": asked_at} if provider != "codex" else {}),
        }
        logger.info("Permission broker: %s wants %s (%s)",
                    sid[:12] or "?", tool_name, request_id)
        self._count_on_session(sid, "permission_asks")
        self._log_permission(self._permission_requests[request_id],
                             "permission_ask")
        self._schedule_display_push()
        return {"hold": 0 if provider == "codex" else int(HOOK_PROMPT_HOLD_SECONDS)}

    def _handle_permission_poll(self, msg: dict) -> dict:
        """The broker asking whether anyone has answered yet.

        `gone` is the script's cue to stop and let the terminal's dialog carry
        on alone; it covers both "that row has been reaped" and "you are not
        the process that asked". A verdict is delivered exactly once — the row
        is popped as it is handed over — so a second poll on the same id gets
        `gone` and cannot answer the dialog twice.
        """
        request_id = str(msg.get("request_id") or "")
        row = self._permission_requests.get(request_id)
        if row is None or row.get("via") != "hook":
            return {"verdict": "gone"}
        if not hmac.compare_digest(str(row.get("claim") or ""),
                                   str(msg.get("claim") or "")):
            logger.info("Permission poll for %s refused: wrong claim",
                        request_id)
            return {"verdict": "gone"}
        row["last_poll_at"] = time.time()
        verdict = row.get("verdict")
        if verdict:
            self._permission_requests.pop(request_id, None)
            self._schedule_display_push()
            return {"verdict": verdict}
        return {"verdict": None}

    async def answer_permission(self, request_id: str,
                                behavior: str) -> tuple[bool, str]:
        """Allow or deny a tool call from anywhere in Dark Army.

        The local dialog in the session's own terminal stays open through this
        and either answer wins — whichever lands first, per the channel
        contract. So the honest failure here is not "denied" but "too late",
        and it is reported rather than swallowed: on refusal nothing else on
        screen moves, which is the lesson `stop_session` already paid for.
        """
        request = self._permission_requests.get(request_id)
        if request is None:
            return False, "That prompt has already been answered."
        if behavior not in ("allow", "deny"):
            return False, f"Unknown verdict: {behavior}"
        # The run-health line's refusal count is stepped only where the
        # verdict is known to have landed — just before each `return True`
        # below — never up here, where the channel leg can still come back
        # "no longer listening" and the hook leg can still refuse a lapsed
        # poll: a refusal nobody received is not a refusal the run hit.
        # On the loop, and never in `_log_permission`: that funnel also
        # runs on the executor under `_reap_permissions`, and the executor
        # must not write `_session_states`.
        sid = str(request.get("session_id") or "")
        if request.get("via") == "codex-hook":
            return False, ("Answer this in its Codex terminal — this Dark Army build "
                           "shows Codex asks but does not answer them yet.")
        if request.get("via") == "grok":
            handle = self._pty_handle_for(sid)
            if handle is None:
                return False, PROMPT_NOT_HOSTED_NOTE
            if self._pty_exited(handle):
                return False, TERMINAL_EXITED_REFUSAL
            path = str(request.get("events_path") or "")
            if grok_events.permission_pending(path) is not True:
                return False, "That prompt has already been answered."
            if sid in self._answering:
                return False, "That terminal is already being answered."
            screen = self._pty.screen(handle)
            digit = prompt_dialogs.grok_choice(screen.text() if screen else [],
                                               behavior)
            if digit is None:
                return False, ("The terminal is not showing the dialog Dark Army "
                               "expected — answer it there.")
            self._answering.add(sid)
            try:
                if not self._pty.write(handle, digit):
                    return False, TERMINAL_EXITED_REFUSAL
                if not await self._pty.drain(handle, TERMINAL_DRAIN_SECONDS):
                    return False, TERMINAL_BUSY_REFUSAL
                deadline = time.monotonic() + GROK_ANSWER_CONFIRM_SECONDS
                while time.monotonic() < deadline:
                    if grok_events.permission_pending(path) is False:
                        self._permission_requests.pop(request_id, None)
                        self._log_permission(request, "permission_resolved",
                                             outcome=behavior)
                        if behavior == "deny":
                            self._count_on_session(sid, "permission_denied")
                        self._schedule_display_push()
                        return True, ""
                    await asyncio.sleep(0.5)
                return False, ("The key was pressed but Grok has not recorded "
                               "an answer — check the terminal.")
            finally:
                self._answering.discard(sid)
        if request.get("via") == "hook":
            # No channel to push down: the broker is polling, so the verdict
            # is *staged* and collected on its next poll, at most a couple of
            # seconds away. Best-effort in the same sense the channel leg is,
            # with one honest refusal — a poll that lapsed means the script
            # has gone, so this tap would never be collected and saying so
            # beats a success nothing acts on.
            if (time.time() - float(request.get("last_poll_at") or 0)
                    > HOOK_PROMPT_POLL_LAPSE_SECONDS):
                return False, "That session is no longer listening."
            request["verdict"] = behavior
            # Logged at the stage, not when `_handle_permission_poll` hands
            # it over: the decision is now, and the poll must not be a second
            # site writing the same line.
            self._log_permission(request, "permission_resolved",
                                 outcome=behavior)
            # Deliberately no `_schedule_display_push()` here, where the
            # channel leg below does push: staging a verdict does not end the
            # prompt. It is genuinely still open — the terminal's dialog is up
            # and the row is real — until the next poll collects it, and that
            # poll pops the row and pushes then. Pushing now would blank the
            # prompt on every surface a second or two before it was answered.
            if behavior == "deny":
                self._count_on_session(sid, "permission_denied")
            return True, ""
        ok = await self._send_to_channel(
            request["port"],
            {"type": "permission_verdict", "request_id": request_id,
             "behavior": behavior})
        # Dropped either way. If the channel has gone, the session it belonged
        # to has gone with it, and a prompt nobody can answer must not sit on
        # screen forever.
        self._permission_requests.pop(request_id, None)
        self._schedule_display_push()
        if not ok:
            self._log_permission(request, "permission_resolved",
                                 outcome="lapsed",
                                 why="the session was no longer listening")
            return False, "That session is no longer listening."
        self._log_permission(request, "permission_resolved", outcome=behavior)
        if behavior == "deny":
            self._count_on_session(sid, "permission_denied")
        return True, ""

    def _count_on_session(self, session_id: str, key: str) -> None:
        """Step one of the run-health counters (`run_health.COUNTER_KEYS`)
        on a session Dark Army holds state for. **Loop thread only** — the three
        call sites are the two ask registrations, the deny verdict and the
        `StopFailure` branch, all on the loop; `_collect_agent_stubs` copies
        the values onto the stub and the executor reads the copy. A session
        with no state is not created for this: a counter is bookkeeping
        about a session, never evidence of one. `sessions.json` carries the
        keys with the rest of the state; an older build reads past them
        and this one defaults them to 0."""
        state = self._session_states.get(session_id or "")
        if state is None:
            return
        try:
            state[key] = int(state.get(key) or 0) + 1
        except (TypeError, ValueError):
            state[key] = 1

    def _hook_prompt_turn_ended(self, row: dict) -> bool:
        """Has the hooked row's session demonstrably moved past the ask?

        True only when the session's own state is one of
        `HOOK_PROMPT_TURN_OVER_STATES` **and** its `last_event` is later than
        `asked_at`. Both halves are load-bearing: the state alone says nothing
        about *when*, and the clock alone is moved by the `Notification` that
        Claude Code fires while the dialog is still up.

        A session Dark Army holds no state for is not judged here — that is the poll
        lapse's job, and answering "the turn ended" for a session we cannot see
        would drop a live ask on no evidence.
        """
        state = self._session_states.get(row.get("session_id") or "")
        if not state:
            return False
        return (state.get("state") in HOOK_PROMPT_TURN_OVER_STATES
                and state.get("last_event", 0) > row.get("asked_at", 0))

    def _codex_prompt_turn_ended(self, row: dict) -> bool:
        sid = row.get("session_id") or ""
        rec = self._codex_records.get(sid)
        if rec is None:
            # Closed or aged out: the record is dropped and the tombstone
            # written. No record without a tombstone may simply be one not
            # read yet, so that row waits for the longstop.
            return sid in self._finished
        return bool(rec and rec.last_event > row.get("asked_at", 0)
                    and (not rec.turn_active
                         or rec.current_tool != row.get("tool_name")))

    def _reap_permissions(self) -> None:
        """Drop prompts that are no longer blocking anything.

        `answer_permission` was the only remover, which covers exactly one of
        the ways a prompt ends — answered *in Dark Army*. The ordinary case is the
        user answering the dialog in their own terminal, and the harness
        resolves that locally without telling us. A permission row is
        deliberately not dismissible (dismissing it would leave the session
        blocked with nothing on screen saying why), so a leaked one pins its
        session into "Needs you" for the life of the daemon — the exact failure
        that section exists to prevent.

        Three signals, in order of how much they prove:

        * **The channel is gone.** The session went with it; nobody can answer.
          A hook-brokered row has no channel, so its equivalent is **the
          broker stopped polling** (`HOOK_PROMPT_POLL_LAPSE_SECONDS`), with
          its own `hold_until` behind that: the script polls every two seconds
          while it holds, so silence means it exited, and it exits for exactly
          the reasons a prompt ends. `hold_until` is now
          `asked_at + PERMISSION_STALE_SECONDS` — **the hold and the longstop
          below are one figure**, and this rung reaches it first, so an
          expired hook row is always dropped with "the broker's hold ran
          out" and the longstop speaks for channel rows alone. Half an hour
          rather than half a minute because the dialog was watched staying
          drawn and answerable through a hold
          (`docs/2026-09-07-permission-hold-verification.md`); the poll lapse
          did not move with it and is still what does the work.
        * **The hooked session's turn ended.** The rung below reads
          `working`/`thinking` only, which is a *resume*; a desk answer often
          produces no resume at all — the tool runs, the turn finishes, and the
          session goes `idle`. `_hook_prompt_turn_ended` is the hook leg's own
          version, over `HOOK_PROMPT_TURN_OVER_STATES`, and it exists because
          `hold_until` is half an hour now: without it a row answered at the
          desk while the script kept polling shadowed the session's next real
          ask for that whole window. Deliberately not "any newer event" — see
          the constant, where the `Notification` companion is the reason.
        * **The session resumed.** A prompt is a stop. If the session is
          working or thinking again on an event *later* than the ask, the
          answer already happened somewhere else. Deliberately not "any newer
          event": `Notification` fires while a session sits blocked, so a
          bare `last_event` comparison would clear a prompt that is still up.
        * **A longstop TTL**, generous on purpose — a genuinely blocked
          session waiting for someone who stepped away is the case Dark Army is
          *for*, so this is a leak guard, not a policy.

        Iterates a copied key list: this runs on the executor thread while
        `_handle_channel_message` writes the same dict on the loop.
        """
        if not self._permission_requests:
            return
        now = time.time()
        live_ports = set(self._live_channels())
        for rid in list(self._permission_requests):
            row = self._permission_requests.get(rid)
            if row is None:
                continue
            why = ""
            hooked = row.get("via") == "hook"
            if hooked:
                # No channel, so no port to test. The broker's own poll is the
                # substitute: it comes back every two seconds while it holds,
                # so a lapse means the script exited — answered at the desk,
                # given up, or the session died. The hold is its own longstop.
                if now - float(row.get("last_poll_at") or 0) > \
                        HOOK_PROMPT_POLL_LAPSE_SECONDS:
                    why = "the session stopped asking about it"
                elif now > float(row.get("hold_until") or 0):
                    why = "the broker's hold ran out"
                elif self._hook_prompt_turn_ended(row):
                    why = "the session's turn ended without it"
            elif row.get("via") == "codex-hook":
                if self._codex_prompt_turn_ended(row):
                    why = "Codex moved past the ask"
            elif row.get("via") == "grok":
                pending = grok_events.permission_pending(
                    str(row.get("events_path") or ""))
                if pending is False:
                    why = "Grok recorded the answer"
            elif row.get("port") not in live_ports:
                why = "its channel went away"
            if why:
                # The strongest signal already spoke; the two below are the
                # weaker ones and have nothing to add.
                self._permission_requests.pop(rid, None)
                logger.info("Dropping permission prompt %s: %s", rid, why)
                if not row.get("verdict"):
                    self._log_permission(row, "permission_resolved",
                                         outcome="lapsed", why=why)
                continue
            if now - row.get("asked_at", now) > PERMISSION_STALE_SECONDS:
                why = "it went unanswered too long"
            else:
                sid = (row.get("session_id", "") if row.get("via") in
                       ("hook", "codex-hook", "grok") else
                       (self._channel_session(row["port"])
                        or row.get("session_id", "")))
                state = self._session_states.get(sid) or {}
                if (state.get("state") in ("working", "thinking")
                        and state.get("last_event", 0) > row.get("asked_at", 0)):
                    why = "the session answered it and moved on"
            if why:
                self._permission_requests.pop(rid, None)
                logger.info("Dropping permission prompt %s: %s", rid, why)
                if not row.get("verdict"):
                    self._log_permission(row, "permission_resolved",
                                         outcome="lapsed", why=why)

    def _permission_snapshot(self) -> list[dict]:
        """The open prompts, oldest first — the order they blocked in.

        Reaped by *reading*, the same way `_live_channels` expires: every
        consumer of the prompts comes through here, so there is nowhere for a
        dead one to hide, and no extra timer to keep alive."""
        self._reap_permissions()
        rows = sorted(self._permission_requests.values(),
                      key=lambda r: r.get("asked_at", 0))
        out = []
        for row in rows:
            entry = dict(row)
            if row.get("via") in ("hook", "codex-hook", "grok"):
                entry["session_id"] = row.get("session_id", "")
            else:
                entry["session_id"] = (self._channel_session(row["port"])
                                       or row.get("session_id", ""))
            entry.pop("port", None)
            # The broker's own bookkeeping, and it stops here. `claim` is a
            # secret and `/api/state` reads are ungated, so publishing it
            # would let any local process collect the verdict and leave the
            # phone's tap doing nothing; the other three are internals no
            # surface has a use for.
            for private in ("claim", "verdict", "last_poll_at", "hold_until",
                            "events_path"):
                entry.pop(private, None)
            via = row.get("via")
            entry["provider"] = row.get("provider") or (
                "grok" if via == "grok" else "codex" if via == "codex-hook"
                else "claude")
            if via == "codex-hook":
                entry["answerable"] = False
                entry["answer_note"] = (
                    "Answer this in its Codex terminal — this Dark Army build "
                    "shows Codex asks but does not answer them yet.")
            elif via == "grok":
                handle = self._pty_handle_for(entry["session_id"])
                entry["answerable"] = bool(handle and not self._pty_exited(handle))
                entry["answer_note"] = "" if entry["answerable"] else PROMPT_NOT_HOSTED_NOTE
            else:
                entry["answerable"] = True
                entry["answer_note"] = ""
            out.append(entry)
        return out

    def _prompts_by_session(self) -> dict[str, dict]:
        """The oldest open permission prompt per *resolved* session.

        The alert policy's view of the relay. Oldest per session because that
        is the prompt the session is actually stopped on. For a channel row
        that holds outright — the channel serialises them. For a hook row it
        holds only as far as three rungs elsewhere keep a spent row from
        surviving, and they are worth naming because a hold is half an hour
        now: a hook row that outlived its answer is drawn here **in place of**
        the session's next genuine ask, and `alerts._permission` then keys its
        "already buzzed" memory on the spent request id, so the live ask raises
        no banner at all.

        The three, weakest last:

        * **A newer ask for the same session** (`_decide_permission_ask` pops
          every earlier hook row before minting). Proof, not inference, and
          the only rung that closes two asks inside one turn.
        * **The poll lapse** (`_reap_permissions`,
          `HOOK_PROMPT_POLL_LAPSE_SECONDS`) — the script stopped coming back,
          so it exited. Fifteen seconds wide.
        * **`_hook_prompt_turn_ended`** — the session moved to a
          `HOOK_PROMPT_TURN_OVER_STATES` state on a later event. Read-driven,
          so it needs a snapshot pass to land on the right moment.

        None of the three is instant, so this is *narrowed*, not closed: a
        gap of up to a snapshot pass between a desk answer and the pop is
        possible, and a session Dark Army holds no state for falls to the poll lapse
        alone.

        Only resolved sessions, because an alert that cannot name its
        agent cannot be muted, suppressed, or revealed; an unresolved prompt
        simply waits here until the pid joins up, and fires then. Read from the
        executor thread during enrichment, which is the same standing race the
        cards copy on the line above it already accepts.
        """
        out: dict[str, dict] = {}
        for row in self._permission_snapshot():
            sid = row.get("session_id") or ""
            if sid and sid not in out:
                out[sid] = row
        return out

    def _session_title(self, session_id: str, transcript_path: str) -> str:
        """Human-friendly session name for the card, cached and
        invalidated by the transcript's mtime. Falls back to a short id.

        The cache is read *after* the generated title rather than before it: a
        name minted by `session_title.py` arrives without touching the
        transcript, so the mtime this cache is keyed on has not moved, and a
        card built a second earlier would keep quoting the opening prompt for
        the rest of the session."""
        if not transcript_path:
            grok_title = grok_roster.title_of(grok_roster.read_summary(session_id))
            if grok_title:
                return grok_title
            return session_id[:8] if session_id else ""
        try:
            mtime = os.path.getmtime(transcript_path)
        except OSError:
            mtime = 0.0
        generated = self._titles_shop.title_for(session_id)
        cached = self._ai_title_cache.get(session_id)
        if not generated and cached and cached[0] == mtime and cached[1]:
            return cached[1]
        name = session_display_name(transcript_path, session_id, generated)
        self._ai_title_cache[session_id] = (mtime, name)
        return name

    def _enter_state(
        self, session_id: str, state: str, tool_name: str, now: float, *, create: bool,
    ) -> None:
        """Put a session in `state`, on `tool_name`, as of `now`. Without
        `create` a session Dark Army does not hold stays absent, so a late event
        cannot bring back one that has ended."""
        entry = self._session_states.get(session_id)
        if entry is None:
            if not create:
                return
            entry = self._session_states[session_id] = {}
        entry.update(state=state, tool_name=tool_name, last_event=now)

    # Events that prove the dialog is no longer on screen. Everything else a
    # session can emit mid-wait — a subagent finishing, a compact — says
    # nothing about the question, and dropping it on those would blank the row
    # for the rest of the wait. `tool_use` is on the list and also the thing
    # that *sets* it: the ask tool is filtered out before the pop.
    QUESTION_CLEARING_EVENTS = frozenset({
        "tool_use", "tool_done", "tool_failed", "permission", "add", "dismiss",
        "session_start",
    })

    @staticmethod
    def _clamped_reading(raw) -> dict:
        """One dialog, re-cut, in the shape `_pending_questions` holds.

        The flat first question's own keys stay top-level — an older daemon
        reading this back from `sessions.json` after a downgrade rebuilds
        exactly the keys it knows and drops the rest — with the whole
        `questions` list riding inside under its own key. `clamp_questions`
        is the tolerance seam: it takes a current hook's list, an older
        installed hook's flat-only message, or a persisted flat dict, and a
        reading we cannot make anything of is `{}`.
        """
        questions = clamp_questions(raw)
        if not questions:
            return {}
        reading = {k: v for k, v in questions[0].items() if k != "index"}
        reading["questions"] = questions
        return reading

    def _restore_pending_questions(self, stored: dict[str, dict]) -> None:
        """Take back the questions the last run was holding, and only those.

        Two conditions, and both are about whether the dialog can still be up.
        The session has to have **survived the startup prune** — a question
        belonging to a session we just decided was dead is a row nobody can
        answer — and it has to have come back in the `waiting` state, which is
        the state `AskUserQuestion` itself sets and the only one an unanswered
        dialog leaves behind.

        Neither condition can prove the human did not answer while the daemon
        was down; nothing can, since the event that would have said so was
        delivered to a socket that was not listening. That is the same
        best-effort the whole hook path runs on, and it self-corrects on the
        next event the session emits: every clearing event in
        `QUESTION_CLEARING_EVENTS` pops the slot. A stale question for one tool
        call is a smaller wrong than a blank row for the length of a wait,
        which is what this replaces. Re-clamped on the way in for
        `clamp_question`'s stated reason — what is on disk was cut by whichever
        copy of the hook handler wrote it.
        """
        for sid, raw in (stored or {}).items():
            state = self._session_states.get(sid)
            if state is None or state.get("state") != "waiting":
                continue
            question = self._clamped_reading(raw)
            if question:
                self._pending_questions[sid] = question
        if self._pending_questions:
            logger.info(
                "Restored %d pending question(s) across restart: %s",
                len(self._pending_questions),
                ", ".join(sid[:12] for sid in self._pending_questions),
            )

    def _track_pending_question(self, event: str, session_id: str,
                                msg: dict) -> None:
        """Hold the question a PreToolUse just reported, until it is answered.

        The hook is the only source that has it *while it matters* — the
        transcript gets the asking turn when the answer does. Kept as its own
        pass rather than folded into `_update_session_state`, which already
        turns this same event into the `waiting` state: that method is the
        state machine, and one more dict keyed on the same event does not
        belong inside it.
        """
        if not session_id or event not in self.QUESTION_CLEARING_EVENTS:
            return
        tool_name = msg.get("tool_name", "")
        if event == "tool_use" and is_ask_user_question(tool_name):
            # Re-cut here: what arrived was trimmed by whichever copy of the
            # hook handler is installed on disk, which may be older than this
            # daemon. A payload we cannot read clears the slot rather than
            # leaving the previous question standing under a new dialog.
            had = self._pending_questions.pop(session_id, None)
            question = self._clamped_reading(msg)
            if question:
                self._pending_questions[session_id] = question
                capture = getattr(self, "_decisions", None)
                if capture is not None:
                    root, project = self._session_place(session_id)
                    capture.question_hook(session_id,
                        dict(root=enrollment.root_enrolled(root), project=project,
                             session_id=session_id, source_id=question.get("id"),
                             kind="question", questions=question.get("questions") or [question],
                             question_text=question.get("text"), provenance="observed hook question"))
            # Written through to disk here rather than left to the state
            # machine's own persist: the two run off the same event, but only
            # this one knows whether the map actually changed, and a question
            # that is not on disk before the next restart is the whole bug.
            if question or had:
                self._persist_sessions()
            return
        # PermissionRequest fires for AskUserQuestion too — Claude Code sends
        # it as a companion to the very same PreToolUse, milliseconds behind
        # (see `_update_session_state`'s `permission` branch, which reuses the
        # ask-question `waiting` semantics for exactly this reason). That is
        # not proof the dialog left the screen; treating it as such was
        # popping the question `tool_use` had just set, so the row sat under
        # "Needs you" showing a stale card or last-said line for the entire
        # wait instead of the question — observed on a live session: 8 minutes
        # with the right answer typed 50ms too early. A permission event for
        # any *other* tool still clears, per the class docstring above.
        #
        # And Claude Code sends it *twice*: `PermissionRequest` names the tool,
        # but the `Notification` that follows a few seconds later
        # (`notification_type=permission_prompt`, converted in `protocol.py`)
        # has no tool field in its payload at all, so it arrives here naming
        # nothing. That second event is what re-opened this bug — measured on
        # a live session: question recorded at 11:15:10.856, popped at
        # 11:15:16.933 by the nameless companion, and never restored, because
        # the transcript only carries the asking turn once the answer does. A
        # permission event that names no tool is evidence about no tool: it
        # cannot contradict the question standing in the slot, so it is not
        # allowed to clear it.
        if event == "permission" and (not tool_name
                                      or is_ask_user_question(tool_name)):
            return
        # The idle_prompt Notification converts to an `add` too (protocol.py
        # stamps it `hook="Notification"`), and Claude Code fires it ~60s into
        # any quiet spell — a standing AskUserQuestion dialog is exactly that,
        # so the reminder was popping the question a minute into every wait.
        # Same reasoning as the nameless permission companion above: an event
        # that names no tool is evidence about no tool and cannot contradict
        # the question in the slot. A Stop's or StopFailure's `add` still
        # clears — those mark the turn ending, which takes the dialog with it.
        if event == "add" and msg.get("hook") == "Notification":
            return
        if self._pending_questions.pop(session_id, None) is not None:
            capture = getattr(self, "_decisions", None)
            if capture is not None:
                capture.question_clear(session_id)
            self._persist_sessions()

    def _update_session_state(
        self, event: str, hook: str, session_id: str,
        agent_id: str = "", tool_name: str = "", pid: Optional[int] = None,
        parked: bool = False, quiet: bool = False, subagent_type: str = "",
        origin_stamp: str = "",
    ) -> bool:
        """Apply one hook event to the state Dark Army keeps for `session_id`.

        The per-event work is `_STATE_STEPS[event]` (an event with no step
        still refreshes the bookkeeping below); what every event then does,
        whichever it was, is `_after_state_step`. The answer is whether the
        change is worth writing to `sessions.json`: the state moved, the live
        subagent set changed, or the session is gone. A clock alone is not.

        `parked` and `quiet` are the caller's verdicts on a Stop or
        Notification, and only the `add` step reads them. Parked: the turn is
        waiting on background work, so neither a card nor a state change is
        due (`_parked_reason`). Quiet: the turn ended on a work report or is
        Mission Control's reply, so the row goes to `idle` even for a
        Notification, whose `confused` would otherwise bank it under Needs
        you a minute later.

        `origin_stamp` is where the session says it came from
        (`BOB_COMPANION_ORIGIN`), kept from the first message that carried
        one and never replaced: it is fixed at the spawn, so a different
        later value is a bug or a forgery, and it attributes rather than
        authorises (`origin.py`).
        """
        if not session_id:
            return False
        before = self._session_states.get(session_id)
        was_state = before["state"] if before else None
        was_live = before.get("subagents", set()).copy() if before else None
        step = _HookStep(hook=hook, agent_id=agent_id, tool_name=tool_name,
                         parked=parked, quiet=quiet, subagent_type=subagent_type,
                         now=time.time(), now_mono=time.monotonic())
        apply = self._STATE_STEPS.get(event)
        if apply is not None and apply(self, session_id, step) is False:
            return False
        after = self._after_state_step(session_id, step, pid, origin_stamp)
        if after is None:
            return before is not None
        return (after["state"] != was_state
                or after.get("subagents", set()) != (was_live or set()))

    def _after_state_step(self, session_id: str, step, pid, origin_stamp: str):
        """What follows every event for a session that still exists; returns
        its state, or None when the event removed (or never made) it."""
        entry = self._session_states.get(session_id)
        if entry is None:
            # A step that declined to create (a stray `permission` for an
            # ended session) leaves nothing to bring back to life.
            return None
        # It exists again, so any record of it having ended is now wrong.
        self._finished.pop(session_id, None)
        self._closed_ids.pop(session_id, None)
        self._closed_codex_turns.pop(session_id, None)
        # `busy_since` spans a stretch of work. A wait for the human is a
        # pause inside the stretch; any other resting state ends it.
        if entry["state"] in ("working", "thinking"):
            entry.setdefault("busy_since", step.now)
        elif entry["state"] != "waiting":
            entry.pop("busy_since", None)
        if pid is not None:
            entry["pid"] = pid
        entry["last_event_monotonic"] = step.now_mono
        # Absent until a message carries one, so an unattributed session
        # stores and publishes exactly what it did before origins existed.
        if origin_stamp and not entry.get("origin"):
            entry["origin"] = origin_stamp[:origin.MAX_STAMP_CHARS]
        return entry

    def _touch_session(self, session_id: str, now: float) -> None:
        entry = self._session_states.get(session_id)
        if entry is not None:
            entry["last_event"] = now

    def _step_session_start(self, session_id: str, step) -> None:
        # The only step that replaces the whole entry, and the only place
        # `prompted` is written False: a session watched from its very start
        # is the one kind known never to have been asked anything. Every
        # other entry lacks the key, which `_parked_reason` reads as asked.
        # The origin survives the replacement, so a second SessionStart
        # cannot re-attribute a session that already said where it is from.
        fresh = {"state": "registered", "last_event": step.now, "prompted": False}
        kept = (self._session_states.get(session_id) or {}).get("origin")
        if kept:
            fresh["origin"] = kept
        self._session_states[session_id] = fresh

    def _step_tool_use(self, session_id: str, step) -> None:
        # A question to the person is a wait, anything else is work. A tool
        # call may be the first we hear of a session, so it may create one,
        # and it proves somebody prompted it even if that hook was lost.
        asking = is_ask_user_question(step.tool_name)
        self._enter_state(session_id, "waiting" if asking else "working",
                          step.tool_name, step.now, create=True)
        self._session_states[session_id]["prompted"] = True

    def _step_tool_done(self, session_id: str, step) -> None:
        # Registered for the ask tools only: the person has answered, so a
        # waiting row resumes thinking. A PostToolUse for a session we do not
        # hold is ignored.
        entry = self._session_states.get(session_id)
        if entry is None:
            return
        if entry["state"] == "waiting" and is_ask_user_question(step.tool_name):
            entry["state"] = "thinking"
        entry["last_event"] = step.now

    def _step_permission(self, session_id: str, step) -> None:
        # Stopped on a "may I?" dialog. Nothing says when it was granted, so
        # the next tool call, stop or prompt moves the row on. A real ask is
        # always preceded by its PreToolUse, so this never creates a session.
        self._enter_state(session_id, "waiting", step.tool_name, step.now, create=False)

    def _step_tool_failed(self, session_id: str, step) -> None:
        # A tool that failed outright is a snag, lighter than an API error,
        # and raises no card. Never creates a session.
        self._enter_state(session_id, "confused", step.tool_name, step.now, create=False)

    def _step_compact(self, session_id: str, step) -> None:
        self._touch_session(session_id, step.now)

    def _step_add(self, session_id: str, step) -> None:
        entry = self._session_states.setdefault(
            session_id, {"state": "idle", "last_event": step.now})
        # A parked turn keeps its state (it is still working); only the
        # clock moves. StopFailure is never parked.
        if step.parked:
            pass
        elif step.hook == "Stop" or step.quiet:
            entry["state"] = "idle"
        elif step.hook == "Notification":
            entry["state"] = "confused"
        elif step.hook == "StopFailure":
            already_failing = entry.get("state") == "error"
            entry["state"] = "error"
            # One run-health step per failed turn, but one diary line per
            # fall into error: a flapping API retries into this every time.
            self._count_on_session(session_id, "stop_failures")
            if not already_failing:
                self._log_session_event(session_id, "session_error")
        entry["last_event"] = step.now

    def _step_dismiss(self, session_id: str, step) -> None:
        if step.hook == "SessionEnd":
            # The one route that knows the session ended on purpose.
            self._forget_session(session_id, "ended")
        elif step.hook == "UserPromptSubmit":
            self._step_prompt(session_id, step.now)
        else:
            self._touch_session(session_id, step.now)

    def _step_prompt(self, session_id: str, now: float) -> None:
        entry = self._session_states.setdefault(
            session_id, {"state": "thinking", "last_event": now})
        entry.update(state="thinking", last_event=now, prompted=True)
        # The person moved the session on: the wait they dismissed (or
        # settled from a card) is over, so the next one must list and buzz.
        store = getattr(self, "_inbox_acks", None)
        if store is not None:
            try:
                store.forget_waiting(session_id)
            except Exception:  # pragma: no cover - a hide never fails a hook
                logger.debug("forget_waiting %s failed", session_id[:8], exc_info=True)
        # The harness takes no prompt while a child of the last turn still
        # runs, so whatever children it had are done, including any whose
        # SubagentStop never arrived and any async ones still owed an answer.
        # Only the live bookkeeping goes; `subagents_seen` records what ran.
        if entry.get("subagents"):
            entry["subagents"] = set()
        for key in ("subagent_event_at", "subagents_async", "async_park_at"):
            entry.pop(key, None)

    def _step_subagent_start(self, session_id: str, step):
        if not step.agent_id:
            return False
        entry = self._session_states.setdefault(
            session_id, {"state": "working", "last_event": step.now})
        live = entry.setdefault("subagents", set())
        live.add(step.agent_id)
        # A unique id supersedes the role name an earlier hook may have used
        # as the key for this same child.
        if step.subagent_type and step.subagent_type != step.agent_id:
            live.discard(step.subagent_type)
        # `subagents_seen` is the record of which roles ran, in first-seen
        # order, deduped and bounded; the live set cannot serve, because a
        # stage that starts and stops between two snapshots is never in it.
        # The role is kept rather than the id: it is what the stage track
        # draws (Claude Code's id already is its role).
        ran = entry.setdefault("subagents_seen", [])
        role = step.subagent_type or step.agent_id
        if role not in ran and len(ran) < MAX_SEEN_SUBAGENTS:
            ran.append(role)
        # `subagent_event_at` is the parking clock `_parked_reason` reads.
        entry["last_event"] = entry["subagent_event_at"] = step.now

    def _step_subagent_stop(self, session_id: str, step) -> None:
        entry = self._session_states.get(session_id)
        if entry is None:
            return
        live = entry.get("subagents")
        if live is not None:
            live.discard(step.agent_id)
            if step.subagent_type and step.subagent_type != step.agent_id:
                live.discard(step.subagent_type)
        if step.agent_id:
            # A stop hook is not proof the child finished: an async child's
            # fires when the spawning turn ends and it works on (see
            # `subagent_watch`). So the id and the moment of its stop are
            # kept, for `_async_subagent_reason` to compare with the child's
            # own transcript when a card is next due. The map is rebuilt from
            # whatever shape a damaged file left (a bare list would make the
            # store below raise, and every later stop would be lost with it).
            owed = entry.get("subagents_async")
            if not isinstance(owed, dict):
                owed = {aid: step.now for aid in (owed or ()) if isinstance(aid, str)}
            entry["subagents_async"] = owed
            if step.agent_id in owed or len(owed) < MAX_SEEN_SUBAGENTS:
                owed[step.agent_id] = step.now
        entry["last_event"] = entry["subagent_event_at"] = step.now

    #: event name -> the step `_update_session_state` applies for it. A step
    #: returning False abandons the event before any shared bookkeeping.
    _STATE_STEPS = {
        "session_start": _step_session_start,
        "tool_use": _step_tool_use,
        "tool_done": _step_tool_done,
        "permission": _step_permission,
        "tool_failed": _step_tool_failed,
        "compact": _step_compact,
        "add": _step_add,
        "dismiss": _step_dismiss,
        "subagent_start": _step_subagent_start,
        "subagent_stop": _step_subagent_stop,
    }

    def _parent_of_child(self, session_id: str) -> str:
        """The session that spawned `session_id` as a subagent, or "".

        Grok children are addressed by their own session id — they have a
        session directory on disk and they fire their own hooks — but they are
        not sessions on any surface: a child that opens a row takes a nickname,
        a face and a project of its own, so one terminal is drawn as two agents
        with two different names, and the tab (named after the parent, which
        owns the tty) disagrees with the panel. That is the mismatch this
        answers.

        The mapping needs no new bookkeeping. A Grok parent's live-subagent set
        is keyed by the child's *session id*, from `SubagentStart`'s
        `subagent_id` and again from the `subagent_id:` spawn records
        `grok_roster.live_subagent_ids` reads out of `chat_history.jsonl` — so
        the parent is simply the row that already holds this id. Exactly one
        owner, or none: two parents claiming one child is not something Grok
        can produce, and guessing between them would move somebody else's work
        onto the wrong row.

        Claude Code is unaffected either way. Its subagents never emit hooks
        under their own id, and its `agent_id`s are types (`bc-implementer`),
        which no session id can equal.
        """
        if not session_id:
            return ""
        # Over a `list(...)` snapshot: this is reached from the executor (via
        # `_claiming_session_ids` and the binders) while the loop mutates
        # `_session_states`, and a live dict iteration there raises
        # "dictionary changed size during iteration".
        owners = [
            sid for sid, state in list(self._session_states.items())
            if sid != session_id and session_id in (state.get("subagents") or ())
        ]
        return owners[0] if len(owners) == 1 else ""

    def _resolve_subagent_parent(self, session_id: str, msg: dict) -> str:
        """Session whose live-set this start/stop should mutate.

        Grok fires SubagentStop on the child. The protocol remaps when
        ``parentSessionId`` is present; older hook scripts do not. A child
        id that is not a fleet row then belongs to the one parent that
        already holds this agent_id. Two parents with the same type stay
        untouched — guessing would steal a badge from the wrong row.
        """
        parent = msg.get("parent_session_id") or ""
        if parent:
            return parent
        # The child's own stop, fired inside the child: `01a00e3d` arrives as
        # both the session and the agent_id. Asked before the "is it a row we
        # drew" test below, because a stray child row — one opened by a daemon
        # from before `_parent_of_child`, restored out of sessions.json —
        # answers that test yes and swallows the stop.
        owner = self._parent_of_child(session_id)
        if owner:
            return owner
        if session_id in self._session_states or session_id in self._grok_records:
            return session_id
        agent_id = msg.get("agent_id") or ""
        if not agent_id:
            return session_id
        owners = [
            sid for sid, state in self._session_states.items()
            if agent_id in (state.get("subagents") or ())
        ]
        if len(owners) == 1:
            return owners[0]
        return session_id

    def _sync_grok_subagents(self, session_id: str, state: dict) -> None:
        """Replace a Grok parent's hook set with who chat_history says is live.

        The hook set is a lifetime of types when stops land on the child.
        ``live_subagent_ids`` is None when the parent never spawned — leave
        the hook set, including Claude-shaped ids, alone.
        """
        rec = self._grok_records.get(session_id)
        cwd = (rec.cwd if rec is not None else "") or state.get("cwd") or ""
        live = grok_roster.live_subagent_ids(session_id, cwd)
        if live is None:
            return
        new = set(live)
        if (state.get("subagents") or set()) != new:
            state["subagents"] = new

    def _apply_grok_live_subagents(self, snapshot: dict) -> None:
        """Copy chat-reconciled Grok live-sets onto session state.

        Enrich runs off the loop and must not touch `_session_states`.
        The snapshot it returns already has the right `subagent_ids`;
        this writes them back so the strip and parked-turn test agree.
        """
        changed = False
        for rows in snapshot.values():
            if not isinstance(rows, list):
                continue
            for entry in rows:
                if not entry.pop("_subs_reconciled", False):
                    continue
                sid = entry.get("session_id")
                state = self._session_states.get(sid)
                if state is None:
                    continue
                new = set(entry.get("subagent_ids") or [])
                if (state.get("subagents") or set()) != new:
                    state["subagents"] = new
                    changed = True
        if changed:
            self._notify_activity()

    def _apply_grok_finished_demote(self, snapshot: dict) -> None:
        """Idle hook state and drop leftover Stop cards for finished Grok.

        Enrich runs off the loop and must not touch `_session_states` or
        `_active_notifications`. The snapshot it returns already has the
        sleeping bucket; this writes them back so the strip's activity
        counts and the panel's cards agree. Sync, loop-only — do not
        await ``dismiss_notification``. ``_wake_surfaces`` would schedule
        another agents push from inside ``_push_agents_snapshot``; the
        strip/panel pair here is ``_notify_activity`` plus the
        notification observer.
        """
        changed = False
        dropped = False
        for rows in snapshot.values():
            if not isinstance(rows, list):
                continue
            for entry in rows:
                if not entry.pop("_grok_finished_demote", False):
                    continue
                sid = entry.get("session_id")
                state = self._session_states.get(sid)
                card = self._active_notifications.get(sid) or {}
                # Enrich tagged off the loop. A StopFailure, or a prompt
                # whose last_event moved during the hop, must not be
                # overwritten to idle. Stuck `working` after a Work done
                # report is the Grok case Claude's Stop hook already
                # covers — idle it so Close can show.
                seen = entry.pop("_grok_finished_seen_event", None)
                hook_state = state.get("state") if state is not None else ""
                if card.get("hook") == "StopFailure" or hook_state == "error":
                    continue
                if hook_state in ("working", "thinking"):
                    current = state.get("last_event") if state is not None else None
                    if (seen is not None and current is not None
                            and current > seen):
                        continue
                if state is not None:
                    state["state"] = "idle"
                    state["tool_name"] = ""
                if card.get("hook") in PARKED_CARD_HOOKS:
                    self._active_notifications.pop(sid, None)
                    dropped = True
                changed = True
        if dropped:
            self._notify_observers(
                "on_notification_change", self._notification_snapshot()
            )
        if changed:
            self._notify_activity()

    @staticmethod
    def _parked_on_async_child(state: dict, now_mono: float) -> bool:
        """A session held by `_async_subagent_reason` or
        `_background_task_reason` is not a dead session.

        Parking leaves `state` at `working` and raises no card, so
        `_waiting_on_human` — which asks `categorize()` — says False and the
        300-second evictor takes it. But an async child may legitimately write
        nothing for the length of one tool call (a full `pytest` run here is
        ~7 minutes), and its parent emits nothing at all while it waits. The
        parent would go to *Recently finished* with its ask still to come.

        The exemption cannot outlive its reason: it is stamped only where the
        rung actually parked, and expires on `subagent_watch.STALE_SECONDS` —
        the same ceiling that releases the park itself.
        """
        parked_at = state.get("async_park_at")
        if not isinstance(parked_at, (int, float)):
            return False
        return now_mono - parked_at <= subagent_watch.STALE_SECONDS

    def _evict_stale_sessions(self) -> None:
        """Forget every session that has said nothing for longer than the
        staleness timeout, unless its silence is expected.

        Silence is read off the monotonic clock, so waking a Mac from sleep
        does not age every session at once. A parent's live subagents work
        through the parent's own tool calls, so a quiet parent means quiet
        children. Expected silence is a session waiting on the person
        (`_waiting_on_human`, the same waiting bucket `categorize()` fills: a
        card, or `waiting` / `error`; `confused` only beside a card), since nobody at the desk
        means no events and a dead process is still caught by the liveness
        check; and one parked on an async child (`_parked_on_async_child`).
        """
        now_mono = time.monotonic()
        limit = self._session_staleness_timeout

        def expired(sid, entry):
            quiet_for = now_mono - entry.get("last_event_monotonic", now_mono)
            return (quiet_for > limit
                    and not self._waiting_on_human(sid, entry)
                    and not self._parked_on_async_child(entry, now_mono))

        doomed = [sid for sid, entry in self._session_states.items() if expired(sid, entry)]
        for sid in doomed:
            logger.info("Session %s went quiet past the timeout; forgetting it", sid[:12])
            self._forget_session(sid, "evicted")
        if doomed:
            self._persist_sessions()

    async def _dismiss_evicted_cards(self, cards_before: set) -> None:
        """Tell the observers about cards an eviction took with it.

        The evictors drop entries from `_active_notifications` without
        announcing it, so the card list the menu bar and panel hold would
        keep a card for a session that no longer exists. `cards_before` is
        the set of card keys from before the eviction; any difference means
        one fresh card snapshot goes out."""
        dropped = cards_before - set(self._active_notifications)
        if not dropped:
            return
        for sid in dropped:
            logger.info("Evicted session %s still had a card — dismissing", sid[:12])
        if self._observers:
            self._notify_observers("on_notification_change", self._notification_snapshot())

    def _persist_sessions(self) -> None:
        save_sessions(self._session_states, self._sessions_path,
                      self._pending_questions)

    # --- History (see history.py) ---

    def _history_write(self, method: str, *args, **kwargs) -> None:
        """Fire-and-forget a history write into the executor.

        sqlite3 blocks, and this runs on the event loop that everything else
        shares. Failures are logged inside HistoryStore; the executor hop exists
        only so the write never happens inline."""
        if self._history is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return          # no running loop (a test calling a handler directly)
        loop.run_in_executor(
            None, functools.partial(getattr(self._history, method), *args, **kwargs)
        )

    # --- The event log -------------------------------------------------------

    def _log_event(self, kind: str, **fields) -> None:
        """Fire-and-forget one diary line (`event_log.EventLog.append`).

        `_history_write`'s shape with one deliberate difference. That helper
        *returns* when there is no running loop, because its callers are all
        on the loop; this one is also called from the executor — the bind
        expiries inside `_reconcile_board`, `_reap_permissions` under the
        snapshot, `_enrich_agent_stubs` itself — where `get_running_loop`
        raises. Returning there would silently drop every
        `card_dispatch_failed`, so off the loop the append is made directly:
        it is already on a worker thread and the write is the point."""
        # `getattr`, not the attribute: several tests build a daemon with
        # `__new__` and never run `__init__`, the `__dict__.setdefault`
        # convention `_enrich_agent_stubs` already follows.
        log = getattr(self, "_event_log", None)
        if log is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.append(kind, **fields)
            return
        loop.run_in_executor(None, functools.partial(log.append, kind, **fields))

    def _start_already_on_record(self, sid: str, entry: dict) -> bool:
        """True when the diary already holds this row's finish: a session
        stale-evicted and returned under the same id (or a roster stub that
        comes back after its run was tombstoned) carries a `started_at`
        older than the `session_end` on record, and a second start for the
        same run would be a lie. No `started_at`, or none on record, is a
        start."""
        log = getattr(self, "_event_log", None)
        if log is None:
            return False
        started = entry.get("started_at")
        if not isinstance(started, (int, float)):
            return False
        ended = log.last_ts(sid, "session_end")
        return ended is not None and float(started) < ended

    def _log_session_event(self, sid: str, kind: str, *,
                           project: Optional[str] = None,
                           title: Optional[str] = None, **detail) -> None:
        """A diary line about one session, with the display fields the row
        already wears: `project` through `_session_place` (the finished stub
        and the hook state behind it), `nickname` from `_nicknames_shown`,
        `title` from `_names_shown` — the same ladder and the same maps the
        agents snapshot publishes, never a second resolution. A caller that
        knows better (the enrich loop, holding the very row) passes the two
        display fields in. `detail` is **named keys only**, never a row."""
        if getattr(self, "_event_log", None) is None:
            return
        tomb = self._finished.get(sid) or {}
        stub = tomb.get("stub") or {}
        state = self._session_states.get(sid) or {}
        if project is None:
            project = self._session_place(sid)[1] or stub.get("project", "") \
                or str(state.get("project") or "")
        if title is None:
            title = getattr(self, "_names_shown", {}).get(sid, "")
        provider = str(state.get("provider") or stub.get("provider") or "")
        if provider and not detail.get("provider"):
            detail["provider"] = provider
        self._log_event(
            kind, project=project or "",
            nickname=self._nicknames_shown.get(sid, ""),
            title=title or "", session_id=sid, detail=detail)

    def _log_permission(self, row: dict, kind: str, **extra) -> None:
        """A diary line about one permission prompt, from **named keys** off
        the row and never the row itself: the broker's row carries `claim`
        (a secret) and the channel's carries `port` (internal), and
        `EventLog.append` would refuse either — this is the first fence."""
        capture = getattr(self, "_decisions", None)
        if capture is not None and isinstance(row, dict):
            root, project = self._session_place(str(row.get("session_id") or ""))
            capture.permission(row, kind, root, project, extra)
        if getattr(self, "_event_log", None) is None or not isinstance(row, dict):
            return
        detail = {
            "request_id": str(row.get("request_id") or ""),
            "tool": str(row.get("tool_name") or ""),
            "via": row.get("via") or "channel",
        }
        if kind == "permission_ask":
            detail["description"] = str(row.get("description") or "")
        detail.update({k: v for k, v in extra.items() if v})
        self._log_session_event(str(row.get("session_id") or ""), kind, **detail)

    # --- The board ---------------------------------------------------------
    # Every board verb — the store calls, dispatch/refine/queue, the
    # reconcile, the channel-request handlers — lives in
    # `daemon_board.BoardVerbsMixin` (a pure move; see that module's
    # docstring). The module-level constants they read stay defined here.

    def _sample_metrics(self, session_id: str, data: dict) -> None:
        """Persist a statusline reading, at most once per session per interval.

        The source ticks on every assistant message; none of cost, context fill or
        rate-limit budget moves fast enough to justify a row each time, and at
        eight agents that would be a row a second, forever."""
        if self._history is None:
            return
        now = time.monotonic()
        last = self._last_metric_sample.get(session_id, 0.0)
        if now - last < METRIC_SAMPLE_INTERVAL:
            return
        self._last_metric_sample[session_id] = now
        self._history_write("record_metrics", session_id, dict(data))
        # The cost here is Claude Code's own figure, not our arithmetic — the one
        # number in this system that does not need a disclaimer.
        if data.get("cost_usd") is not None:
            self._history_write(
                "upsert_session", session_id,
                cost_usd=data.get("cost_usd"), cost_source=history.COST_MEASURED,
                title=data.get("session_name") or None,
                primary_model=data.get("model_id") or None,
                cwd=data.get("cwd") or None,
                last_seen=time.time(),
            )

    def _record_grok_history(self, session_id: str, metrics: dict,
                             title: str = "") -> None:
        """Persist a Grok row's reported cost. Runs inside the enrich executor,
        so this writes directly — ``_history_write`` needs the event loop.

        ``cost_source`` is measured: Grok stamped the ticks, we did not
        estimate from a price table.
        """
        if self._history is None or not session_id or not metrics:
            return
        now = time.monotonic()
        last = self._last_metric_sample.get(session_id, 0.0)
        if now - last < METRIC_SAMPLE_INTERVAL:
            return
        self._last_metric_sample[session_id] = now
        self._history.record_metrics(session_id, dict(metrics))
        if metrics.get("cost_usd") is not None:
            self._history.upsert_session(
                session_id,
                cost_usd=metrics.get("cost_usd"),
                cost_source=history.COST_MEASURED,
                title=title or None,
                primary_model=metrics.get("model_id") or None,
                cwd=metrics.get("cwd") or None,
                last_seen=time.time(),
                provider="grok",
            )

    def _attention_reason(self, state: Optional[str], event: str,
                          hook: str, tool_name: str) -> str:
        """Why a session ended up where it did — the part a bare state name loses.

        "waiting" alone cannot tell you whether an agent is holding a permission
        dialog for `rm -rf` or politely asking a multiple-choice question, and that
        difference is the whole point of an attention queue."""
        if state == "waiting":
            if event == "permission":
                return f"permission:{tool_name}" if tool_name else "permission"
            if is_ask_user_question(tool_name):
                return ASK_USER_QUESTION_TOOL
            return hook or event
        if state == "error":
            return "api_error"
        if state == "confused":
            return "tool_failed" if event == "tool_failed" else "idle_prompt"
        return ""

    def _record_transition(self, session_id: str, state_before: Optional[str],
                           event: str, hook: str, tool_name: str) -> None:
        if not session_id or self._history is None:
            return
        session = self._session_states.get(session_id)
        if session is None:
            # The session just ended. Close the story rather than dropping it.
            if state_before is not None:
                now = time.time()
                self._history_write("record_state", session_id, state_before,
                                    "ended", hook or event, now)
                self._history_write("upsert_session", session_id,
                                    ended_at=now, end_reason=hook or event,
                                    last_seen=now)
            return

        state_after = session.get("state")
        if state_after == state_before:
            return
        self._history_write(
            "record_state", session_id, state_before, state_after,
            self._attention_reason(state_after, event, hook, tool_name), time.time(),
        )
        self._history_write("upsert_session", session_id,
                            project=session.get("project", ""),
                            last_seen=time.time())

    def _handle_statusline(self, msg: dict) -> dict:
        """Store a session's live metrics and answer with what the *other* agents
        are doing.

        Deliberately does **not** touch session state. This fires on every
        assistant message, so counting it as activity would make staleness
        eviction meaningless — a session would read as alive for as long as its
        terminal stayed open, which is precisely what eviction exists to catch."""
        data = flatten_statusline(msg.get("data") or {})
        sid = data.get("session_id") or msg.get("session_id") or ""
        if sid:
            data["received_at"] = time.time()
            self._session_metrics[sid] = data
            self._sample_metrics(sid, data)
            # In memory, and on a much finer grain than the history row above:
            # this one exists to answer "at this rate", which is a question
            # about the last few minutes and cannot be asked of a database
            # sampled once a minute for the record.
            self._samples.add(sid, time.time(), data)
            # Throttled: at ~1 statusline/s across a handful of agents, pushing
            # every time would re-parse transcripts continuously for a cost figure
            # that moves by cents.
            now = time.monotonic()
            if now - self._last_metrics_push >= METRICS_PUSH_INTERVAL:
                self._last_metrics_push = now
                self._schedule_agents_push()

        categories = self._reconciled_categories()
        waiting = [s for s, c in categories.items() if c == "waiting" and s != sid]
        return {
            "others_waiting": len(waiting),
            "working": sum(1 for s, c in categories.items() if c == "running" and s != sid),
            "total": len(categories),
        }

    def _on_agent_records(self, records: list) -> None:
        """Reconciler callback, on the event-loop thread. Stores the records and
        only wakes the surfaces when something they actually render moved — this
        fires every 10s and the overwhelming majority of ticks change nothing."""
        def fingerprint(by_sid):
            return {sid: (r.activity, r.name, r.kind) for sid, r in by_sid.items()}

        new = {r.session_id: r for r in records}
        new = {sid: r for sid, r in new.items() if not self._closed_by_bob(sid)}
        changed = fingerprint(new) != fingerprint(self._agent_records)
        # A background agent has no removal path at all — it simply stops being
        # listed. That disappearance is the only "it finished" signal it will ever
        # emit, so it is what we tombstone on. Hook-tracked sessions are skipped:
        # they have real removal paths that record a truer verdict, and the poll
        # dropping one first would otherwise beat the hook to it with a vaguer one.
        for sid, rec in self._agent_records.items():
            # `_finished` is checked, not just `_session_states`: the poll lags the
            # hooks by up to 10s, so a session that ended cleanly is still listed
            # for one more tick. Without this it would be tombstoned a second time
            # — overwriting "ended" with the vaguer "completed" and, far worse,
            # restarting its clock at zero.
            if sid in new or sid in self._session_states or sid in self._finished:
                continue
            self._record_finished(sid, {"project": rec.project, "pid": rec.pid,
                                        "state": rec.activity}, "completed")
        self._track_blocked_since(new)
        self._agent_records = new
        if changed:
            self._notify_activity()
            self._schedule_agents_push()
            self._schedule_display_push()

    def _track_blocked_since(self, new: dict) -> None:
        """Timestamp when each agent *entered* `blocked`.

        The CLI reports the state but not when it started, and the distinction
        decides whether something is an alert or a leftover. Watching transitions
        gives the real answer for anything that blocks while we are running. For an
        agent already blocked on our very first poll we cannot know, so we assume
        the pessimistic case and date it from session start — an abandoned session
        from three weeks ago then reads as old on the first tick after a restart,
        instead of alarming for another twelve hours."""
        first_poll = not self._agent_records and not self._blocked_since
        for sid, rec in new.items():
            if rec.activity != agents_poll.BLOCKED:
                self._blocked_since.pop(sid, None)
            elif sid not in self._blocked_since:
                previously_known = sid in self._agent_records
                self._blocked_since[sid] = (
                    time.time() if previously_known and not first_poll
                    else (rec.started_at or time.time())
                )
        for sid in list(self._blocked_since):
            if sid not in new:
                del self._blocked_since[sid]

    def _blocked_is_stale(self, sid: str) -> bool:
        since = self._blocked_since.get(sid)
        return since is not None and (time.time() - since) > agents_poll.STALE_BLOCKED_SECONDS

    @staticmethod
    def _codex_left_open(rec) -> bool:
        """A Codex root that finished on close-out's "left open" line: no
        question, not working, no live helpers. `_reconciled_categories`
        files it under `sleeping` rather than `waiting`, and its row's
        `state` reads `idle`, the way the Grok demote does. Narrow on
        purpose — a `## Work done` report alone still asks, since Needs you
        is the only flag an unchecked Codex build gets."""
        return bool(rec.left_open and not rec.stats.question
                    and rec.activity != "working" and not rec.stats.agents)

    def _reconciled_categories(self) -> dict[str, str]:
        """session_id -> waiting | running | sleeping, across *both* sources.

        Hook state is the fine-grained truth for the sessions we track. The
        reconciler (`claude agents --json`) contributes two things it alone knows:

        * sessions the hook stream cannot see at all — background agents never emit
          the terminal-shaped events the rest of the daemon is built around;
        * the `blocked` state, which no hook reports. A blocked background agent is
          waiting on a human and, having no terminal, will wait forever — so it
          outranks whatever the hooks last said about that session.

        Everything else defers to session_stats.categorize()."""
        from . import session_stats as ss

        # Memoised on ROSTER_REFRESH_INTERVAL: the refreshes stat every Codex
        # rollout and walk the process table, and this method runs on the loop
        # from every snapshot and every statusline message. Within the window
        # the last rosters are current enough; the stamp is written *before*
        # refreshing so a refresh that raises does not retry at full cadence.
        # On the loop a lapsed window only *schedules* the refresh — the
        # glob, parse and process scan run on the executor
        # (`_refresh_rosters`) and this call answers from the last rosters.
        # In an executor thread (`_alert_suppressed`) or outside any loop
        # (tests) there is no running loop and it refreshes inline as before.
        now_mono = time.monotonic()
        if (self._roster_refreshed_at is None
                or now_mono - self._roster_refreshed_at
                >= ROSTER_REFRESH_INTERVAL):
            self._roster_refreshed_at = now_mono
            loop = _running_loop()
            if loop is not None:
                loop.call_soon_threadsafe(self._schedule_roster_refresh)
            else:
                self._refresh_grok_records()
                self._refresh_codex_records()
        notif_sids = set(self._active_notifications.keys())
        prompts = self._prompts_by_session()
        out: dict[str, str] = {}
        suppressed_for: Optional[float] = None
        # `list(...)`: `_alert_suppressed` calls this from the executor while
        # the loop mutates `_session_states`.
        for sid, s in list(self._session_states.items()):
            subs = len(s.get("subagents", set()))
            has_card = sid in notif_sids
            since = self._seconds_since_event(s)
            out[sid] = ss.categorize(
                s.get("state"), subs, has_card, seconds_since_event=since
            )
            # Was `waiting` vetoed by the window? Asked by calling the single
            # source of truth a second time without the clock, rather than by
            # restating its waiting rule here — two copies of a rule is how the
            # surfaces learn to disagree. The second call only happens for
            # sessions with a sub-window-fresh event, and only costs a tuple
            # compare. A suppressed wait must wake something later: nothing
            # else recomputes the category once the events stop, which is
            # precisely what a genuine wait does.
            if (since is not None and since < ss.WAITING_HYSTERESIS_SECONDS
                    and out[sid] == "running"
                    and ss.categorize(s.get("state"), subs, has_card) == "waiting"):
                remaining = ss.WAITING_HYSTERESIS_SECONDS - since
                suppressed_for = (remaining if suppressed_for is None
                                  else min(suppressed_for, remaining))
        if suppressed_for is not None:
            self._arm_hysteresis_repush(suppressed_for)
        for sid, rec in self._grok_records.items():
            # Grok writes the asking turn to chat_history while the dialog
            # is up, so `_questions` can be live before (or without) a hook
            # stamping `waiting`. Promote only — a genuine Stop wait with
            # no question stays whatever categorize() already said.
            if self._questions.get(sid):
                out[sid] = "waiting"
                continue
            if sid in out:
                continue
            # Dead-PID leftovers are dropped in `_refresh_grok_records`, so
            # everything still here has a live grok process.
            out[sid] = "running"
        for sid, rec in self._codex_records.items():
            if rec.is_child or self._closed_by_bob(sid):
                continue
            if sid not in out:
                out[sid] = (
                    "waiting" if sid in prompts or rec.stats.question else
                    "running" if rec.activity == "working" or rec.stats.agents
                    else "sleeping" if rec.left_open
                    else "waiting"
                )
        for sid, rec in self._agent_records.items():
            if rec.needs_attention and self._blocked_is_stale(sid):
                # A background agent blocked for days is a tombstone, not a
                # session: `claude agents --json` lists abandoned ones forever.
                # Its own bucket, so it is neither an alert nor a row in the menu,
                # but is still reachable in the panel to be cleaned up. Only
                # reconciler-only records qualify — anything the hooks are also
                # tracking is a live session and keeps its real category.
                if sid not in self._session_states:
                    out[sid] = "abandoned"
                    continue
            if rec.needs_attention and not self._blocked_is_stale(sid):
                out[sid] = "waiting"
            elif sid not in out:
                out[sid] = "running" if rec.activity == agents_poll.BUSY else "sleeping"
        return out

    # ── waiting hysteresis ───────────────────────────────────────────────────
    #
    # A parent whose subagent delivers an event every second or so used to flip
    # between `waiting` and `running` on each one, and the panel draws that as
    # the row hopping in and out of "Needs you" — destroying the view between a
    # mouse-down and its mouse-up, which is why clicks on those rows never
    # landed. The fix is one rule applied at the single categorisation source:
    # a wait is not believed until the session has been quiet for
    # session_stats.WAITING_HYSTERESIS_SECONDS. The complementary card gate
    # lives in _notification_snapshot, so the category and the card can never
    # disagree about whether a row belongs in "Needs you".

    def _session_waiting_on_question(self, session_id: str) -> bool:
        """True when an answer burst may still reach this session's dialog.

        Hook-tracked sessions enter `waiting` on the ask's PreToolUse. Grok
        writes the asking turn to chat_history while the dialog is up, so a
        row the hooks have not stamped can still be blocked — `_questions`
        is that live reading, cleared on the matching tool_result.
        """
        st = self._session_states.get(session_id)
        if st is not None and st.get("state") == "waiting":
            return True
        # A Grok interview can be live on `_questions` before (or without)
        # a hook stamping `waiting`. Claude leftover `_questions` on a
        # session that already moved on must still refuse the burst.
        return bool(
            session_id in self._grok_records
            and self._questions.get(session_id)
        )

    def _waiting_on_human(self, sid: str, state: dict) -> bool:
        """True when this session is parked on a person.

        The eviction exemption and the bucket the panel draws must be the same
        judgement, so it is asked of `session_stats.categorize()` — the single
        source of truth — rather than restated as a state-string tuple here.
        The hysteresis clock is deliberately not handed over: that window is a
        draw-time anti-flap device for a row that has just changed bucket, and
        here it could only ever turn the exemption *off* for a session whose
        last event is seconds old — a session no eviction path is judging
        anyway.

        A live Grok `ask_user_question` is the same park: `_questions` is
        that reading, and without it a hookless interview fell through to
        the short pidless grace and vanished from Needs you.
        """
        from . import session_stats as ss

        if ((state.get("provider") == "grok" or sid in self._grok_records)
                and self._questions.get(sid)):
            return True
        return ss.categorize(
            state.get("state"),
            len(state.get("subagents", set())),
            sid in self._active_notifications,
        ) == "waiting"

    @staticmethod
    def _seconds_since_event(state: dict) -> Optional[float]:
        """Seconds since this session's last hook event, or None if unstamped.

        Monotonic only, deliberately no wall-clock fallback: every event stamps
        `last_event_monotonic` and load re-stamps it (see __init__), so in
        production the stamp is never absent — and `last_event` (wall) can jump
        backwards on an NTP sync, which is the wrong clock to gate an
        interruption with. None means "no reading", which categorize treats as
        "apply no hysteresis"."""
        mono = state.get("last_event_monotonic")
        if mono is None:
            return None
        return time.monotonic() - mono

    def _waiting_hysteresis_remaining(self, session_id: str) -> float:
        """Seconds left before this session's wait may be believed; 0.0 when the
        window is closed, the session is unknown, or it carries no stamp."""
        from . import session_stats as ss

        s = self._session_states.get(session_id)
        if not s:
            return 0.0
        since = self._seconds_since_event(s)
        if since is None:
            return 0.0
        return max(0.0, ss.WAITING_HYSTERESIS_SECONDS - since)

    def _arm_hysteresis_repush(self, remaining: float) -> None:
        """One-shot wake-up for a wait the hysteresis is currently suppressing.

        Without it a genuine wait would stay `running` until the *next* event —
        which, the session being blocked on a human, may never come — or until
        the 10s snapshot refresher, which is the backstop, not the plan.
        Coalesced on the earlier deadline, so a fleet of suppressed sessions
        arms one timer for the first window to close; the callback re-arms for
        the rest. Guarded like _schedule_display_push: unit tests construct
        daemons with no loop, and outside one there is nothing to push to."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # A small pad so the timer fires just after the window closes rather
        # than fractionally inside it, re-arming for nothing.
        delay = max(0.0, remaining) + 0.05
        when = loop.time() + delay
        armed = self.__dict__.get("_hysteresis_timer")
        if armed is not None:
            handle, armed_when = armed
            if not handle.cancelled() and armed_when <= when:
                return
            handle.cancel()
        self._hysteresis_timer = (loop.call_later(delay, self._hysteresis_repush), when)

    def _hysteresis_repush(self) -> None:
        """The window closed: re-read and re-push, so the wait surfaces on its
        own. Runs on the loop. The handle is cleared first, so the snapshot
        calls below can re-arm for any window still open."""
        self._hysteresis_timer = None
        if self._observers:
            self._notify_observers(
                "on_notification_change", self._notification_snapshot())
        self._notify_activity()
        self._schedule_agents_push()

    def _session_provider(self, session_id: str) -> Optional[str]:
        """claude / grok / codex / None when we have not stamped one yet."""
        st = self._session_states.get(session_id)
        if st and st.get("provider") in ("grok", "claude", "codex"):
            return st["provider"]
        if session_id in self._grok_records:
            return "grok"
        if session_id in self._codex_records:
            return "codex"
        return None

    def _session_reachable(self, session_id: str) -> bool:
        """Dark Army can put a line in front of this session.

        Grok never falls through to the Claude channel: the inherited ``bob``
        MCP already starts in Grok sessions and the handshake fails, so a
        Grok ``is_channel`` true would be a box that silently goes nowhere.
        """
        provider = self._session_provider(session_id)
        if provider == "codex":
            return False
        if provider == "grok":
            return self._grok_leader_up and session_id in self._grok_resident
        return self._channel_for_session(session_id) is not None

    def _load_rosters(self) -> tuple:
        """Read both hookless rosters off the loop: `(grok, codex)`.

        Executor only — the Codex half globs and parses rollouts and walks
        the (shared) process table. Each half is `None` when it could not be
        read, which its apply step treats as "leave the roster alone"."""
        try:
            recs = grok_roster.load_active()
        except Exception:
            logger.warning("Grok roster read failed", exc_info=True)
            recs = None
        try:
            records = codex_rollouts.load_recent()
        except Exception:
            logger.warning("Codex rollout scan failed", exc_info=True)
            records = None
        return recs, records

    async def _refresh_rosters(self, *, announce: bool = True) -> None:
        """Refresh the Grok and Codex rosters: read on the executor, apply
        on the loop (the apply mutates `_codex_records`, `_finished`,
        `_hidden_codex` and `_closed_ids`). Only ever run as the one
        `_roster_refresh_task` (`_schedule_roster_refresh`). When the set of
        either roster's ids moved and `announce` is set, the surfaces are
        woken — the caller that scheduled this answered from the old rosters.

        A read is applied only if no roster was applied while it was being
        taken (`_roster_apply_gen`): a direct, inline refresh (the liveness
        sweep, the stop path) that landed meanwhile is fresher, and an older
        read applied over it could readmit a root it had just retired."""
        self._roster_refreshed_at = time.monotonic()
        loop = asyncio.get_running_loop()
        generation = getattr(self, "_roster_apply_gen", 0)
        recs, records = await loop.run_in_executor(None, self._load_rosters)
        if getattr(self, "_roster_apply_gen", 0) != generation:
            return
        before = (frozenset(self._codex_records), frozenset(self._grok_records))
        try:
            if recs is not None:
                self._refresh_grok_records(recs)
            if records is not None:
                self._refresh_codex_records(records)
        except Exception:
            # A scheduled refresh has nobody awaiting it to hear the raise.
            logger.warning("roster refresh failed", exc_info=True)
        self._roster_refreshed_at = time.monotonic()
        after = (frozenset(self._codex_records), frozenset(self._grok_records))
        if announce and after != before:
            self._notify_activity()
            self._schedule_agents_push()

    def _schedule_roster_refresh(self, *, announce: bool = True) -> Optional[asyncio.Future]:
        """Loop only: start one `_refresh_rosters`, never two at once, and
        return the task in flight (the one just started or the one already
        running)."""
        task = getattr(self, "_roster_refresh_task", None)
        if task is not None and not task.done():
            return task
        self._roster_refresh_task = asyncio.ensure_future(
            self._refresh_rosters(announce=announce))
        return self._roster_refresh_task

    def _refresh_codex_records(self, records=None) -> None:
        """Replace the Codex roster, respecting revision-scoped Hide marks.

        `records` is a roster `_load_rosters` already read off the loop;
        absent, this reads it here (the direct callers — the liveness sweep,
        the stop path — ask on purpose and stay unconditional)."""
        self._roster_apply_gen = getattr(self, "_roster_apply_gen", 0) + 1
        if records is None:
            try:
                records = codex_rollouts.load_recent()
            except Exception:
                logger.warning("Codex rollout scan failed", exc_info=True)
                return
        visible = {}
        for record in records:
            sid = record.session_id
            if self._closed_by_bob(sid) and not self._codex_thread_resumed_after_close(record):
                # Presence alone cannot bypass the close-identity protection;
                # the process just closed may still be observed while exiting.
                continue
            # Codex never knocks — Dark Army learns about it by reading `~/.codex`
            # journals — so there is no message to refuse and the row is
            # filtered instead. Same effect as the door, different mechanism,
            # and worth knowing when something goes missing.
            if not enrollment.root_enrolled(record.cwd):
                continue
            if not record.is_child and record.process_seen is False:
                # Do not stamp `_hidden_codex`: the process scan is re-run
                # every refresh, so a resumed thread returns by itself.
                if sid in self._codex_records and sid not in self._finished:
                    self._record_finished(
                        sid, self._codex_finished_state(record), "no process")
                continue
            tomb = self._finished.get(sid)
            # Reachable with the note still set only through the proof above.
            resumed = self._closed_by_bob(sid)
            if (tomb is not None
                    and tomb.get("end_reason") == "no process"
                    and record.process_seen is not True
                    and not resumed):
                # Already retired for having no process, and this scan carries
                # no positive evidence it is back: `_process_snapshot()`
                # returning None for one tick leaves `process_seen` None, and
                # readmitting on that would put a dead root back under *Needs
                # you* and drop its finished row until the next refresh.
                # Failing open means never *retiring* a row on no evidence; it
                # does not mean un-retiring one on none.
                continue
            hidden = self._hidden_codex.get(sid)
            if hidden == (record.path, record.revision):
                continue
            if hidden is not None:
                # Same thread wrote again, or moved to a replacement journal.
                self._hidden_codex.pop(sid, None)
            visible[sid] = record
            self._closed_ids.pop(sid, None)
            self._closed_codex_turns.pop(sid, None)
            if tomb is not None and (
                    resumed
                    or (record.process_seen is True
                        and tomb.get("end_reason") == "no process")
                    or tomb.get("end_reason") == "evicted"):
                # A *proven* resume of a closed thread retires whatever
                # tombstone it holds, whatever the reason. Keeping the
                # "closed" one would leave the retirement branch above unable
                # to write a fresh one when this second run dies, so
                # *Recently finished* would show the first run's cost on the
                # first run's clock and expire on the first run's retention.
                # `evicted` needs no process evidence: it was written for a
                # journal that had gone quiet, and being back inside the live
                # scan *is* the journal writing again.
                self._finished.pop(sid, None)
        self._retire_quiet_codex_roots(visible, records)
        # A stale watermark for a journal no longer in the bounded live scan is
        # still useful: a stale panel request remains idempotent, and if the
        # thread later returns with a different revision the branch above drops
        # it. Do not prune merely because this scan did not see the id.
        self._codex_records = visible
        self._codex_navigation = MappingProxyType({
            sid: proof for sid, proof in self._codex_navigation.items()
            if sid in visible and codex_rollouts.project_title_roots((visible[sid],)) == (proof.root,)
        })

    def _retire_quiet_codex_roots(self, visible: dict, records: list) -> None:
        """Tombstone a Codex root whose journal aged out of the bounded scan.

        `codex_rollouts.load_recent()` drops any journal untouched for
        `LIVE_GRACE_SECONDS` (15 minutes), so the retirement branch above —
        which can only run for a record the scan *returned* — never sees the
        commonest Codex ending of all: the turn finishes, the tab stays open,
        the journal goes quiet, and fifteen minutes later the row simply
        stopped being in `visible`. Nothing was stamped. That cost three
        things a person can see: no *Recently finished* row, no `session_end`
        in the diary (eight Codex starts and no ends, measured 2026-09-09),
        and — because `_consider_work_record` reads the closing words off the
        snapshot row — a board card whose record said *"the run ended without
        the assistant saying anything"* over a Codex session that had written
        a full `## Work done` report into its journal.

        The stamp is **`evicted`**, which is already this daemon's word for
        wall-clock staleness — a statement about Dark Army's bookkeeping rather
        than about the tab — so it stays out of `GROK_END_REASONS`, out of
        `_mid_turn_quiet_ids`' set, and out of the `no process` readmission
        guard. The record is the one Dark Army already holds, so the tombstone
        carries that run's `stats` and its report survives the row.

        Silence is proven per journal rather than assumed from an absent id:
        an unreadable `load_recent()` returns `[]`, and retiring the whole
        roster off that would be retiring on no evidence, which is the one
        thing this file's failure rule forbids. A stat that cannot be taken
        leaves the row alone.

        **The seam is narrowed, not closed.** `load_recent()` also bounds on
        `MAX_FILES` (64) *before* it applies the grace, so a root can leave
        the scan by overflow rather than by age; its journal's mtime is then
        fresh, `quiet` is False, and `self._codex_records = visible` drops it
        silently exactly as before. A journal whose `stat` fails permanently
        goes the same way. Both are the pre-existing behaviour on a machine
        with more than sixty-four recent rollouts, and neither is a licence to
        retire on the absence alone.
        """
        scanned = {getattr(record, "session_id", "") for record in records}
        cutoff = time.time() - codex_rollouts.LIVE_GRACE_SECONDS
        for sid, record in list(self._codex_records.items()):
            if sid in visible or sid in scanned:
                continue
            if record.is_child:
                continue
            # Hide is a person's decision to stop drawing this row; a
            # tombstone would draw it again under another heading.
            if sid in self._hidden_codex or sid in self._finished:
                continue
            try:
                quiet = record.path.stat().st_mtime < cutoff
            except (OSError, AttributeError):
                continue
            if quiet:
                self._record_finished(
                    sid, self._codex_finished_state(record), "evicted")

    def _hide_codex_revision(
        self, session_id: str, record: codex_rollouts.CodexRecord,
    ) -> None:
        """Suppress one exact reading in memory; never signal or touch disk."""
        self._refinement_receipts.pop(session_id, None)
        self._hidden_codex[session_id] = (record.path, record.revision)
        self._codex_records.pop(session_id, None)
        self._codex_navigation = MappingProxyType({
            sid: proof for sid, proof in self._codex_navigation.items() if sid != session_id
        })

    def hide_codex_session(self, session_id: str) -> tuple[bool, str]:
        """Hide a current root Codex row until its journal changes."""
        if session_id in self._session_states or session_id in self._agent_records \
                or session_id in self._grok_records:
            return False, "only Codex rollout rows can be hidden"
        record = self._codex_records.get(session_id)
        if record is None:
            if session_id.startswith("codex:"):
                self._schedule_agents_push()
                return True, "already hidden"
            return False, "no such Codex session"
        if record.is_child:
            return False, "Codex child rows cannot be hidden"
        self._hide_codex_revision(session_id, record)
        self._notify_activity()
        self._schedule_agents_push()
        return True, "hidden until this thread changes"

    def request_preference(self, key: str, value) -> tuple[bool, str]:
        """A phone has asked the Mac to change one preference. Hand it over.

        This verb **applies nothing and stores nothing**. `preferences.json`
        is written by the menu-bar app, which is also the process that
        re-feeds both dials into the daemon at startup — that is what makes a
        change survive a restart — so a second writer here would be a value
        applied but never stored: a setting that dies at the next launch.
        The daemon's whole part is to check the key against a chosen list and
        pass the request to whoever owns settings.

        The 200 this earns therefore means *accepted*, not *applied*: the app
        hops to its own thread, stores, applies and republishes, and the
        board snapshot carrying the new value is the proof. The phone holds
        its switch across that gap rather than flicking back for one poll.
        """
        name = str(key or "")
        if name not in PHONE_PREFERENCES:
            return False, f"{name or 'that'} is not a preference the phone may set"
        if not self._observers_implementing("on_preference_request"):
            return False, PREFERENCE_UNREACHABLE_REFUSAL
        self._notify_observers("on_preference_request", name, value)
        return True, "asked the Mac to change that"

    @staticmethod
    def _codex_finished_state(record: codex_rollouts.CodexRecord) -> dict:
        """Synthetic `_record_finished` state for a Codex root that just ended."""
        return {
            "project": record.project, "cwd": record.cwd,
            "pid": record.pid, "state": "idle",
            "provider": "codex", "last_event": record.last_event,
            "_codex_stats": record.stats,
            "_codex_metrics": record.metrics,
            "review_reports": [dict(report) for report in record.review_reports],
            "review_reports_omitted": record.review_reports_omitted,
        }

    def _settle_codex_stop(self, session_id: str, reason: str = "stopped", *, observed=None) -> None:
        """Stamp the journal only after its process is gone, then repaint."""
        record = self._codex_records.get(session_id)
        try:
            latest = observed if observed is not None else next(
                (item for item in codex_rollouts.load_recent()
                 if item.session_id == session_id),
                None,
            )
        except Exception:
            latest = None
        record = latest or record
        synthetic = None
        if record is not None and session_id not in self._finished:
            synthetic = self._codex_finished_state(record)
        if record is not None:
            self._hide_codex_revision(session_id, record)
        else:
            self._codex_records.pop(session_id, None)
        if synthetic is not None:
            self._record_finished(session_id, synthetic, reason)
        if reason == "closed":
            ident = record.process_identity if record is not None else None
            self._note_closed(
                session_id,
                pid=ident.pid if ident is not None else (
                    record.pid if record is not None else None),
                create_time=ident.create_time if ident is not None else None,
            )
        self._notify_activity()
        self._schedule_agents_push()

    def stop_codex_session(self, session_id: str) -> tuple[bool, str]:
        """SIGTERM only a uniquely owned native `codex resume <thread>` CLI."""
        record = self._codex_records.get(session_id)
        if record is None:
            return False, "no such Codex session"
        identity = record.process_identity
        if identity is None or identity.match_kind != "explicit_resume":
            return False, "this Codex session has no controllable CLI identity"
        proc = codex_rollouts.matching_process_identity(
            record,
            identity,
            destructive=True,
            records=list(self._codex_records.values()),
        )
        if proc is None:
            if not psutil.pid_exists(identity.pid):
                self._settle_codex_stop(session_id)
                return True, "already stopped"
            return False, "Codex process identity changed; no signal was sent"
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            self._settle_codex_stop(session_id)
            return True, "already stopped"
        except (psutil.AccessDenied, OSError) as exc:
            return False, f"could not stop Codex PID {identity.pid}: {exc}"

        logger.info("stopping Codex session %s (SIGTERM to PID %d)",
                    session_id[:18], identity.pid)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            asyncio.ensure_future(self._confirm_codex_stop(session_id, identity))
        return True, "stopping"

    def _codex_process_state(
        self,
        record: codex_rollouts.CodexRecord,
        identity: codex_rollouts.CodexProcessIdentity,
    ) -> tuple[str, Optional[object]]:
        """same/gone/inaccessible/changed, without ever guessing ownership."""
        proc = codex_rollouts.matching_process_identity(
            record,
            identity,
            destructive=True,
            records=list(self._codex_records.values()),
        )
        if proc is not None:
            return "same", proc
        try:
            current_started = psutil.Process(identity.pid).create_time()
        except psutil.NoSuchProcess:
            return "gone", None
        except (psutil.AccessDenied, OSError):
            return "inaccessible", None
        if abs(current_started - identity.create_time) > 0.01:
            return "gone", None
        return "changed", None

    def _codex_record_for_confirm(
        self,
        session_id: str,
        identity: codex_rollouts.CodexProcessIdentity,
    ) -> Optional[codex_rollouts.CodexRecord]:
        """The journal to confirm against, even if a refresh dropped the roster row."""
        record = self._codex_records.get(session_id)
        if record is not None:
            return record
        try:
            latest = next(
                (item for item in codex_rollouts.load_recent()
                 if item.session_id == session_id),
                None,
            )
        except Exception:
            latest = None
        if latest is None:
            return None
        # Confirm the process we already signalled, not a later attach.
        latest.process_identity = identity
        return latest

    async def _confirm_codex_stop(
        self,
        session_id: str,
        identity: codex_rollouts.CodexProcessIdentity,
        reason: str = "stopped",
    ) -> None:
        """Escalate only after replaying the complete Codex CLI identity."""
        for delay in (0.4, 0.8, 1.5, 3.0):
            await asyncio.sleep(delay)
            if not psutil.pid_exists(identity.pid):
                self._settle_codex_stop(session_id, reason)
                return

        record = self._codex_record_for_confirm(session_id, identity)
        if record is None:
            logger.warning("not escalating Codex %s: rollout record disappeared",
                           session_id[:18])
            self._settle_codex_stop(session_id, reason)
            return
        state, proc = self._codex_process_state(record, identity)
        if state == "gone":
            # The PID disappeared or now belongs to somebody else. Settle the
            # requested journal without touching the recycled owner.
            self._settle_codex_stop(session_id, reason)
            return
        if state != "same":
            logger.warning("not escalating Codex %s: process identity %s",
                           session_id[:18], state)
            return
        try:
            proc.kill()
            logger.info("sent SIGKILL to Codex PID %d (%s)",
                        identity.pid, session_id[:18])
        except psutil.NoSuchProcess:
            self._settle_codex_stop(session_id, reason)
            return
        except (psutil.AccessDenied, OSError) as exc:
            logger.warning("could not SIGKILL Codex PID %d: %s", identity.pid, exc)
            return

        for delay in (0.2, 0.5, 1.0):
            await asyncio.sleep(delay)
            record = self._codex_record_for_confirm(session_id, identity)
            if record is None:
                self._settle_codex_stop(session_id, reason)
                return
            state, _ = self._codex_process_state(record, identity)
            if state == "gone":
                self._settle_codex_stop(session_id, reason)
                return
            if state != "same":
                logger.warning(
                    "not settling Codex %s after SIGKILL: process identity %s",
                    session_id[:18], state,
                )
                return
        logger.warning("Codex session %s survived SIGKILL (PID %d)",
                       session_id[:18], identity.pid)

    def codex_usage_snapshot(self) -> dict:
        """Current Codex account windows for the menu bar and HTTP API."""
        # Usage is read by worker threads.  The roster and revision-scoped Hide
        # watermarks are event-loop-owned, so a worker must not refresh or
        # otherwise adopt rollout records while the loop is changing them.
        return codex_rollouts.usage_snapshot()

    def _on_grok_residents(self, ids: frozenset[str]) -> None:
        if ids == self._grok_resident:
            return
        self._grok_resident = ids
        self._schedule_agents_push()

    def _on_grok_leader_down(self) -> None:
        self._grok_leader_up = False
        self._grok_resident = frozenset()
        self._schedule_agents_push()

    async def _grok_leader_watcher(self) -> None:
        """Loop-owned: connect when the sock is there, close when it is gone.

        Seconds, not hooks. A failed connect backs off inside LeaderClient
        so a flapping sock cannot fork a client per tool event.
        """
        while self._running:
            try:
                await self._sync_grok_leader()
            except Exception:
                logger.warning("grok leader watch failed", exc_info=True)
            await asyncio.sleep(LEADER_WATCH_SECONDS)

    async def _sync_grok_leader(self) -> None:
        leader = self._leader
        if leader is None:
            return
        present = grok_leader.leader_sock_present()
        if present and not leader.connected:
            ok = await leader.connect()
            self._grok_leader_up = ok
            if ok:
                self._seed_grok_residents()
                self._grok_resident = leader.residents
                self._schedule_agents_push()
        elif not present and (leader.connected or self._grok_leader_up):
            await leader.close()
            self._grok_leader_up = False
            self._grok_resident = frozenset()
            self._schedule_agents_push()

    def _refresh_grok_records(self, recs=None) -> None:
        """Re-read ~/.grok/active_sessions.json into `_grok_records`.

        The file lags. Grok keeps a closed session listed with a PID that
        still looks like grok (another tab, the `--no-exit-on-disconnect`
        leader, a recycled number) and does not always fire SessionEnd. A
        vanished PID used to become a sleeping leftover "until Grok drops
        it from the file" — which is how an agent sat on the panel for 1h41m
        after the tab closed. Drop those here, and forget hook state for
        any Grok session the roster no longer stands behind.

        `recs` is a roster `_load_rosters` already read off the loop;
        absent, this reads the file here.
        """
        self._roster_apply_gen = getattr(self, "_roster_apply_gen", 0) + 1
        if recs is None:
            recs = grok_roster.load_active()
        if recs is None:
            # Parse failure / missing / non-list: do not update. An
            # unreadable file is not "everyone left".
            return
        file_ids = {r.session_id for r in recs}
        # Grok's roster is a file too, so it is filtered rather than refused —
        # `_refresh_codex_records`' case exactly. Applied before the liveness
        # pass so an unenrolled row never reaches `_grok_records` at all.
        recs = [r for r in recs if enrollment.root_enrolled(r.cwd)]
        live = self._live_grok_records(recs)
        now = time.time()
        departed = set(self._grok_records) - set(live)
        for sid, state in list(self._session_states.items()):
            if state.get("provider") != "grok":
                continue
            if sid in live:
                departed.discard(sid)
                # Back on the live roster: the tab is there. The stay
                # paths below are what stamps; this is the only clear.
                state["tab_gone"] = False
                continue
            if sid in file_ids:
                # In the file, rejected from live (cwd mismatch, dead pid,
                # shared-pid leftover). Waiting / live-pid must not resurrect
                # a listing the roster pass already classified — that pid is
                # some other grok, and Stop would SIGTERM it.
                departed.add(sid)
                continue
            # Absent from the file: lag or brand-new. Exemptions apply.
            if self._grok_hook_state_stays(state, now, sid, live):
                departed.discard(sid)
                continue
            departed.add(sid)
        for sid in departed:
            self._forget_departed_grok(sid)
        # Forget the suppress-list entry once Grok itself drops the row,
        # so a later resume of the same id can come back.
        self._grok_ended.intersection_update(file_ids)
        self._grok_records = live
        for sid, state in self._session_states.items():
            if state.get("provider") == "grok" or sid in live:
                self._sync_grok_subagents(sid, state)
        self._seed_grok_residents()

    def _seed_grok_residents(self) -> None:
        """Hand the live Grok roster to the leader client as residents.

        A no-op unless the leader is connected and its session list cannot
        say who is live (`LeaderClient.flagless`); then the tabs Dark Army
        sees running are the reachable ones, instead of none until a Grok
        session happens to open or close.
        """
        leader = getattr(self, "_leader", None)
        if leader is None or not getattr(leader, "flagless", False):
            return
        leader.seed_residents(getattr(self, "_grok_records", {}) or {})

    def _grok_hook_state_stays(
        self, state: dict, now: float, sid: str,
        live: dict[str, grok_roster.GrokRecord],
    ) -> bool:
        """Waiting, or a live grok pid that still belongs to this session,
        or still inside the roster grace.

        Only for ids *absent* from the roster file (lag / brand-new). A
        listing the live pass already rejected is departed, not delayed.

        A live pid is not enough on its own. `/clear` starts a new session
        in the same tab and Grok does not fire SessionEnd, so the old id
        drops out of the file while its TUI pid is now the *new* session's.
        That leftover sat in Needs you after wrap-up (01a010bc, 17 Aug).
        It returns False and is not stamped: the pid belongs to the new
        session.

        Every stay stamps `tab_gone`. The exception is a never-listed id
        still inside the roster-file lag (`_grok_absent_id_is_roster_lag`):
        a brand-new session the file has not caught up to. A fresh
        `last_event` is not that lag — hooks rewrite it — so a turn that
        is still receiving hooks, or whose pid still looks like grok,
        is a lost tab once it has been listed or is older than the lag.
        """
        pid = state.get("pid")
        if pid is not None:
            owner = next(
                (rec.session_id for rec in live.values() if rec.pid == pid),
                None,
            )
            if owner is not None and owner != sid:
                return False
        stays = (
            state.get("state") == "waiting"
            or (pid is not None and _still_our_process(pid, "grok"))
            or now - state.get("last_event", now) < GROK_ROSTER_GRACE_SECONDS
            or self._grok_headless_turn_alive(sid, state, now)
        )
        if not stays:
            return False
        if not self._grok_absent_id_is_roster_lag(sid, state, now):
            state["tab_gone"] = True
        return True

    def _grok_absent_id_is_roster_lag(
        self, sid: str, state: dict, now: float,
    ) -> bool:
        """A brand-new id the roster file has never listed, still inside
        the 20s lag.

        Called while `_grok_records` is still the previous pass, so an id
        in it has been listed and this is not lag. The age is `busy_since`,
        set once when the work stretch starts and left alone while hooks
        rewrite `last_event`. A row that has never entered that stretch
        has no such clock; `last_event` is then still its birth, and only
        then is it the lag.
        """
        if sid in self._grok_records:
            return False
        born = state.get("busy_since")
        if not isinstance(born, (int, float)):
            born = state.get("last_event", now)
        if not isinstance(born, (int, float)):
            return True
        return now - born < GROK_ROSTER_GRACE_SECONDS

    def _grok_headless_turn_alive(self, sid: str, state: dict,
                                  now: float) -> bool:
        """A Grok row with no tab and no roster line, still mid-turn.

        True when the hook state says the turn is running and the session's
        own directory — or a live child's — was written within
        `GROK_HEADLESS_GRACE_SECONDS`. The witness is the file, not the
        hook: a parent waiting on a subagent fires no hook for minutes, and
        the hook grace alone ended the row on every such wait.
        """
        if state.get("provider") != "grok":
            return False
        if state.get("state") not in ("working", "thinking"):
            return False
        rec = self._grok_records.get(sid)
        cwd = (rec.cwd if rec is not None else "") or state.get("cwd") or ""
        children = tuple(str(c) for c in (state.get("subagents") or ()))
        last = grok_roster.last_activity(sid, cwd, children)
        if last is None:
            return False
        return now - last < GROK_HEADLESS_GRACE_SECONDS

    def _live_grok_records(
        self, recs: list[grok_roster.GrokRecord],
    ) -> dict[str, grok_roster.GrokRecord]:
        """Roster rows that still name a live Grok session, not a leftover."""
        alive: list[grok_roster.GrokRecord] = []
        for rec in recs:
            if rec.session_id in self._grok_ended:
                continue
            if rec.pid is None or not _still_our_process(rec.pid, "grok"):
                continue
            if rec.cwd:
                proc_cwd = _process_cwd(rec.pid)
                if proc_cwd and _same_cwd(proc_cwd, rec.cwd) is False:
                    # The number is a grok process, just not this session's —
                    # typically another tab that inherited the PID in the file.
                    # Unresolvable paths keep the row (None), same as a cwd
                    # we cannot read.
                    continue
            alive.append(rec)
        by_pid: dict[int, list[grok_roster.GrokRecord]] = {}
        for rec in alive:
            by_pid.setdefault(rec.pid, []).append(rec)
        kept: dict[str, grok_roster.GrokRecord] = {}
        # One TUI pid is one tab. Two ids on it is `/clear`, not two
        # sessions — Grok does not SessionEnd the old one, and keeping
        # every hooked row was how the leftover stayed in Needs you.
        # Newest `opened_at` wins, hooked or not; the rejected id then
        # falls through `_refresh_grok_records` as a file leftover.
        for group in by_pid.values():
            newest = max(group, key=lambda r: r.opened_at or 0.0)
            kept[newest.session_id] = newest
        return kept

    def _forget_departed_grok(self, session_id: str) -> None:
        """Drop a Grok session the roster no longer stands behind."""
        self._grok_ended.add(session_id)
        had_hook_state = session_id in self._session_states
        grok = self._grok_records.get(session_id)
        self._forget_session(session_id, "ended")
        if not had_hook_state and grok is not None:
            last_event = grok.opened_at
            if last_event is None:
                try:
                    last_event = grok_roster.ACTIVE_SESSIONS_PATH.stat().st_mtime
                except OSError:
                    last_event = None
            synthetic = {
                "project": grok.project, "pid": grok.pid, "state": "idle",
                "provider": "grok",
            }
            if last_event is not None:
                synthetic["last_event"] = last_event
            self._record_finished(session_id, synthetic, "ended")
        self._grok_records.pop(session_id, None)
        if had_hook_state:
            self._persist_sessions()

    def delete_abandoned_agent(self, session_id: str) -> tuple[bool, str]:
        """Delete an abandoned background agent's job record. (ok, detail).

        `claude agents --json` lists a background agent forever and the CLI has
        no verb to retire one, so this is the only way a run that stopped
        mid-turn to ask a question — with no terminal, so nobody ever answers —
        ever leaves the panel. It was removed once as "API with no caller"; the
        caller is what was missing, not the capability.

        The category check is the safety property, not a UI nicety: `abandoned`
        is the one bucket that is, by construction, a tombstone — a background
        agent blocked past STALE_BLOCKED_SECONDS with no process behind it.
        Anything else is a session someone is still using, and no sequence of
        panel clicks should be able to delete one out from under its owner.
        Deciding that here rather than in the handler means the rule holds for
        every caller, and is checked against live state at the moment of deletion
        rather than against whatever the panel last rendered.

        Blocking rmtree on the event loop, deliberately: it is a handful of small
        files, and a destructive action has to report whether it worked. Handing
        it to the executor would mean answering "accepted" and leaving the user
        to infer the outcome from a row that may or may not disappear.
        """
        rec = self._agent_records.get(session_id)
        if rec is None:
            return False, "no such agent"
        if self._reconciled_categories().get(session_id) != "abandoned":
            return False, "only abandoned agents can be deleted"

        try:
            removed = jobs_store.delete_job(rec.short_id)
        except jobs_store.JobDeleteError as exc:
            logger.warning("refused to delete job %s: %s", rec.short_id, exc)
            return False, str(exc)

        # Forget it here instead of waiting up to 10s for the reconciler to stop
        # reporting it: the push below is what repaints the panel that asked, and
        # a row surviving its own deletion reads as a failure. Dropping it from
        # `_agent_records` is also what keeps it out of `_finished` — the vanish
        # handler tombstones records it still knows about, and a graveyard row
        # somebody deliberately deleted must not come back as "recently
        # finished".
        self._agent_records.pop(session_id, None)
        self._blocked_since.pop(session_id, None)
        self._agents_poller.poll_soon()
        self._schedule_agents_push()
        logger.info("deleted abandoned agent %s (%s)",
                    rec.short_id, rec.name or session_id)
        return True, "deleted" if removed else "already gone"

    def stop_session(self, session_id: str) -> tuple[bool, str]:
        """SIGTERM a live Claude Code or Grok session. (ok, detail).

        The sibling of `delete_abandoned_agent`, and its safety property is the
        opposite one. Deletion is allowed only for tombstones, because it destroys
        a record; stopping is aimed squarely at *live* sessions — that is the whole
        point — so "is this a session someone is using" cannot be the guard.

        The guard is identity instead: **the PID we hold must still be the
        harness we recorded** (claude or grok). PIDs are recycled, our record can
        be minutes stale, and the blast radius of getting it wrong is a SIGTERM
        delivered to whatever unrelated process inherited the number. So we
        re-verify against the live process table at the moment of signalling,
        using the same rule that identifies a session everywhere else
        (pid_resolver.looks_like_session) — never a bare pid_exists, which only
        proves *something* is there.

        SIGTERM first, always: Claude Code flushes its transcript on the way out,
        and the transcript is what history.py and every stats rollup read. Killing
        it uncleanly would cost the user the session's record to save a second.
        SIGKILL only after that request has demonstrably gone unanswered for ~6s —
        see _confirm_stop, which owns the escalation.
        """
        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            rec = self._agent_records.get(session_id)
            grok = self._grok_records.get(session_id)
            if rec is None and grok is None and st is None:
                # The panel can still be showing a frame from before the
                # roster sweep dropped the row. Refresh once; if it is
                # genuinely gone, that is a success — the click meant
                # "get this off the screen", not "kill a process".
                self._refresh_grok_records()
                grok = self._grok_records.get(session_id)
                st = self._session_states.get(session_id)
                if grok is None and st is None:
                    self._schedule_agents_push()
                    return True, "already stopped"
            if rec is not None:
                pid = rec.pid
            elif grok is not None:
                pid = grok.pid
        if pid is None:
            return False, "no PID on record for this session"

        provider = self._session_provider(session_id)
        try:
            proc = psutil.Process(pid)
            # cmdline() rather than name(): a node-wrapped install reports "node".
            if not pid_resolver.looks_like_session(
                proc.name(), " ".join(proc.cmdline() or []), provider,
            ):
                # Either the session already exited and the PID was reused, or we
                # recorded the wrong one. Refuse loudly — do not signal a stranger.
                logger.warning(
                    "refusing to stop %s: PID %d is not a %s process (%s)",
                    session_id[:12], pid, provider or "claude/grok", proc.name(),
                )
                label = "Grok" if provider == "grok" else "Claude"
                return False, f"PID {pid} is no longer a {label} session"
            # Grok only: "a grok exists at this number" is not "this
            # session's grok". Re-check cwd at fire time against the
            # roster, not the panel snapshot. Claude is unchanged.
            if provider == "grok":
                expected = self._grok_recorded_cwd(session_id)
                if expected:
                    proc_cwd = _process_cwd(pid)
                    if proc_cwd and _same_cwd(proc_cwd, expected) is False:
                        logger.warning(
                            "refusing to stop %s: PID %d is a grok in a "
                            "different tree",
                            session_id[:12], pid,
                        )
                        return False, f"PID {pid} is no longer a Grok session"
            proc.terminate()
        except psutil.NoSuchProcess:
            # Already gone. Forgetting it is the honest outcome, and the row the
            # user clicked disappears either way.
            self._forget_stopped(session_id)
            return True, "already stopped"
        except (psutil.AccessDenied, OSError) as exc:
            return False, f"could not stop PID {pid}: {exc}"

        logger.info("stopping session %s (SIGTERM to PID %d)", session_id[:12], pid)
        # Confirm out of band: SIGTERM is a request, and the process needs a moment
        # to honour it. Answering "stopping" now and repainting when it is actually
        # gone beats blocking the event loop on someone else's shutdown.
        #
        # Only when there is a loop to schedule it on. The signal has already been
        # delivered by this point, so a caller off the event loop — the menu bar's
        # AppKit thread, a test — must still get its answer rather than a
        # RuntimeError; it just waits for the reconciler poll to repaint.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            asyncio.ensure_future(self._confirm_stop(session_id, pid))
        return True, "stopping"

    def _grok_permission_already_resolved(self, session_id: str,
                                          msg: dict) -> bool:
        """True when Grok's own log says the permission this hook announces
        was already allowed — see the `permission` rewrite in
        `_handle_message`. Reads one tail of `events.jsonl` on the loop."""
        cwd = str(msg.get("cwd") or "") or self._grok_recorded_cwd(session_id)
        directory = grok_roster.session_dir(session_id, cwd)
        if directory is None:
            return False
        return grok_events.permission_pending(
            str(directory / "events.jsonl")) is False

    def _register_grok_permission(self, session_id: str, msg: dict) -> None:
        """Publish a real open Grok dialog, never an unreadable log guess."""
        cwd = str(msg.get("cwd") or "") or self._grok_recorded_cwd(session_id)
        directory = grok_roster.session_dir(session_id, cwd)
        if directory is None:
            return
        events_path = str(directory / "events.jsonl")
        if grok_events.permission_pending(events_path) is not True:
            return
        detail = grok_events.permission_request_detail(events_path)
        asked_at = time.time()
        rid = "grok-" + hashlib.sha1(
            (session_id + str(asked_at)).encode("utf-8")).hexdigest()[:16]
        for prior in [key for key, row in self._permission_requests.items()
                      if row.get("via") == "grok"
                      and row.get("session_id") == session_id]:
            self._permission_requests.pop(prior, None)
        description = (str(msg.get("description") or "").strip()
                       or str(detail.get("command") or detail.get("description") or "").strip()
                       or str(msg.get("tool_name") or detail.get("tool_name") or ""))
        row = {"request_id": rid, "session_id": session_id,
               "tool_name": str(msg.get("tool_name") or detail.get("tool_name") or ""),
               "description": clip(description, MAX_PROMPT_DESCRIPTION_CHARS),
               "input_preview": clip(str(msg.get("input_preview") or ""),
                                     MAX_PROMPT_INPUT_CHARS),
               "asked_at": asked_at, "events_path": events_path,
               "via": "grok", "provider": "grok"}
        self._permission_requests[rid] = row
        self._count_on_session(session_id, "permission_asks")
        self._log_permission(row, "permission_ask")
        self._schedule_display_push()

    def _grok_recorded_cwd(self, session_id: str) -> str:
        """cwd the roster last attributed to this Grok session, or ''.

        Live records first; then the file listing, so a leftover the
        live pass rejected still has a tree we can refuse against.
        """
        grok = self._grok_records.get(session_id)
        if grok is not None and grok.cwd:
            return grok.cwd
        recs = grok_roster.load_active()
        if recs:
            for rec in recs:
                if rec.session_id == session_id and rec.cwd:
                    return rec.cwd
        return ""

    def _roster_pid(self, session_id: str) -> Optional[int]:
        """PID from the Claude or Grok live roster, or None."""
        rec = self._agent_records.get(session_id)
        if rec is not None and rec.pid:
            return rec.pid
        grok = self._grok_records.get(session_id)
        if grok is not None and grok.pid:
            return grok.pid
        return None

    def _probe_grok_origin(self, session_id: str, pid: Optional[int]) -> None:
        """Read a Grok session's origin stamp off its own process, once per
        pid, and make it the row's — replacing whatever an earlier build
        stored off the leader's hook environment, or clearing it when the
        process carries none (a `grok` a person typed). The read is one
        `ps` in the executor; the write lands on the loop, is dropped for a
        session forgotten meanwhile, and republishes only on a change.
        """
        if not pid or self._grok_origin_probed.get(session_id) == pid:
            return
        self._grok_origin_probed[session_id] = pid
        loop = asyncio.get_running_loop()

        def apply(fut: "asyncio.Future") -> None:
            stamp = ""
            try:
                stamp = fut.result() or ""
            except Exception:
                return
            state = self._session_states.get(session_id)
            if state is None:
                return
            stamp = stamp[:origin.MAX_STAMP_CHARS] if origin.parse(stamp) else ""
            if (state.get("origin") or "") == stamp:
                return
            if stamp:
                state["origin"] = stamp
            else:
                state.pop("origin", None)
            self._persist_sessions()
            self._schedule_agents_push()

        loop.run_in_executor(None, pid_resolver.process_origin, pid) \
            .add_done_callback(apply)

    def _ensure_session_pid(self, session_id: str, state: dict) -> Optional[int]:
        """Hook PID if it still names this session; else the roster's.

        Grok notify often omits pid, and with `use_leader` it stamps the
        shared `grok agent leader` — or, empirically, the TUI that parented
        that leader, which is some *other* tab. `_grok_session_pid_ok`
        rejects the leader but accepts a sibling TUI, so the hook number
        is not "this session". The roster still has the TUI pid. Trusting
        the hook hid Jump, pointed Stop at the wrong tab, PID-dedup
        evicted every other Grok row, and left every VS Code tab on
        `grok-macos-aarch`. Claude is unchanged: a present hook pid is
        still the session.
        """
        provider = state.get("provider") or (
            "grok" if session_id in self._grok_records else None
        )
        pid = state.get("pid")
        roster = self._roster_pid(session_id)
        if provider == "grok":
            if roster and _grok_session_pid_ok(roster):
                if pid != roster:
                    state["pid"] = roster
                return roster
            if pid and _grok_session_pid_ok(pid):
                owner = next(
                    (rec.session_id for rec in self._grok_records.values()
                     if rec.pid == pid),
                    None,
                )
                if owner is None or owner == session_id:
                    return pid
            # The live leader is not this session. Returning it made
            # liveness treat a live Grok row as "PID gone" (`looks_like_grok`
            # excludes the leader) and evict interviews. A recycled number
            # that now names Dock still has to travel to Stop so it can
            # refuse in words rather than look like "no pid".
            if _is_live_grok_leader(pid):
                state["pid"] = None
                return None
            return roster or pid
        if pid:
            return pid
        if roster:
            state["pid"] = roster
            return roster
        return pid

    def _resolve_session_pid(self, session_id: str) -> Optional[int]:
        """The live Claude PID for a session, or None. Same two-source lookup and
        identity guard as stop_session: state record first, then AgentRecord, then
        re-verify against the process table so a recycled PID can't be acted on."""
        codex = self._codex_records.get(session_id)
        if codex is not None:
            identity = codex.process_identity
            if identity is None:
                return None
            proc = codex_rollouts.matching_process_identity(
                codex,
                identity,
                destructive=False,
                records=list(self._codex_records.values()),
            )
            return identity.pid if proc is not None else None
        pid = None
        st = self._session_states.get(session_id)
        if st:
            pid = self._ensure_session_pid(session_id, st)
        if pid is None:
            pid = self._roster_pid(session_id)
        if pid is None:
            return None
        try:
            proc = psutil.Process(pid)
            if not pid_resolver.looks_like_session(
                proc.name(), " ".join(proc.cmdline() or []),
                self._session_provider(session_id),
            ):
                logger.info(
                    "reveal: PID %d for %s is no longer a session process (%s)",
                    pid, session_id[:12], proc.name(),
                )
                return None
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, OSError):
            # Can't introspect it, but it exists and we recorded it — let the reveal
            # try; the worst case is a no-op when `ps -E` also comes up empty.
            pass
        return pid

    def _navigation_proof_current(self, session_id: str):
        proof = self._codex_navigation.get(session_id)
        record = self._codex_records.get(session_id)
        if (proof is None or record is None or self._closed_by_bob(session_id)
                or self._hidden_codex.get(session_id) == (record.path, record.revision)):
            return None
        roots = codex_rollouts.project_title_roots((record,))
        return proof if roots == (proof.root,) else None

    def _codex_terminal_current(self, session_id: str):
        target = getattr(self, "_codex_terminals", {}).get(session_id)
        record = self._codex_records.get(session_id)
        if (target is None or record is None or self._closed_by_bob(session_id)
                or self._hidden_codex.get(session_id) == (record.path, record.revision)):
            return None
        return target if codex_terminal.roots((record,)) == (target.root,) else None

    async def _refresh_codex_terminals(self):
        task = getattr(self, "_codex_terminal_task", None)
        if task is not None and not task.done():
            return
        captured = codex_terminal.roots(self._codex_records.values())
        now = time.monotonic()
        if (captured == getattr(self, "_codex_terminal_roots", None)
                and now - getattr(self, "_codex_terminals_at", 0) < codex_terminal.REFRESH_SECONDS):
            return
        self._codex_terminal_roots = captured
        self._codex_terminals_at = now
        async def observe():
            targets = await codex_terminal.resolve(captured)
            targets = (targets if captured == codex_terminal.roots(
                self._codex_records.values()) else {})
            self._codex_titles_observed_at = time.monotonic()
            if targets != getattr(self, "_codex_terminals", {}):
                self._codex_terminals = targets
                self._schedule_agents_push()
        self._codex_terminal_task = asyncio.create_task(observe())

    def _legacy_navigation_input(self, session_id: str):
        """Copy only immutable legacy routing facts on the daemon loop."""
        state = self._session_states.get(session_id) or {}
        pid = state.get("pid")
        owners = tuple(rec.session_id for rec in self._grok_records.values() if rec.pid == pid)
        return (session_id, pid, self._roster_pid(session_id),
                self._session_provider(session_id), owners)

    @staticmethod
    def _legacy_navigation_pid(captured):
        """Legacy PID preference and identity check, without writing live state."""
        session_id, pid, roster, provider, owners = captured
        if provider == "grok":
            if roster and _grok_session_pid_ok(roster):
                pid = roster
            elif pid and _grok_session_pid_ok(pid) and (not owners or owners[0] == session_id):
                pass
            else:
                pid = roster or pid
        else:
            pid = pid or roster
        if pid is None:
            return None
        try:
            proc = psutil.Process(pid)
            if not pid_resolver.looks_like_session(
                    proc.name(), " ".join(proc.cmdline() or []), provider):
                return None
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, OSError):
            pass  # existing legacy read-only navigation compatibility
        return pid

    def _navigation_capture(self):
        records = dict(self._codex_records)
        roots = codex_rollouts.project_navigation_roots(records.values())
        proofs = {sid: proof for sid in records
                  if (proof := self._navigation_proof_current(sid)) is not None}
        return records, roots, proofs

    def _navigation_capture_current(self, captured) -> bool:
        records, roots, proofs = captured
        return (records.keys() == self._codex_records.keys()
                and all(self._codex_records.get(sid) is record for sid, record in records.items())
                and roots == codex_rollouts.project_navigation_roots(self._codex_records.values())
                and all(self._navigation_proof_current(sid) == proof
                        for sid, proof in proofs.items()))

    async def _fresh_navigation_targets(self, captured, *, for_board=False):
        _records, roots, proofs = captured
        def observe():
            uncertain = set()
            targets = codex_rollouts.navigation_targets(roots, proofs, for_board, uncertain)
            return targets, frozenset(uncertain)
        targets, uncertain = await asyncio.get_running_loop().run_in_executor(None, observe)
        if not self._navigation_capture_current(captured):
            targets, uncertain = {}, frozenset(r.root.session_id for r in roots)
        return (targets, uncertain) if for_board else targets

    async def _board_request_session_fresh(self, port: int) -> Optional[str]:
        """Only add/attach/close/manual spend fresh native-holder attribution,
        off the loop. Close is here because it is open to Codex and it moves a
        card to Done: the strongest of the claims must not rest on the thinnest
        of the attributions. Manual is here because it is open to Codex and it
        releases the card's place in its project's queue."""
        entry = self._channels.get(port)
        if not entry or entry.get("host") != channel_server.HOST_CODEX:
            return self._board_request_session(port)
        registry = dict(entry)
        def refuse(reason):
            # Bounded diagnostic vocabulary only: never registry secrets, cwd,
            # prompts, argv or journal paths from a caller's session.
            logger.info("board attribution refused: stage=request reason=%s port=%d", reason, port)
            return None

        pid = int(registry.get("pid") or 0)
        cwd = dispatch.normalise_root(str(registry.get("cwd") or ""))
        had_exact_evidence = any(
            proof.pid == pid or dispatch.normalise_root(proof.root.cwd) == cwd
            for proof in self._codex_navigation.values())
        captured = self._navigation_capture()
        try:
            targets, uncertain = await asyncio.wait_for(
                self._fresh_navigation_targets(captured, for_board=True), 5.0)
        except asyncio.TimeoutError:
            return refuse("observation-timeout")
        except (OSError, psutil.Error):
            return refuse("observation-unavailable")
        if self._channels.get(port) is not entry or entry != registry:
            return refuse("registry-changed")
        if not self._navigation_capture_current(captured):
            return refuse("roster-changed")
        exact = [sid for sid, target in targets.items() if target.proof and target.pid == pid
                 and dispatch.normalise_root(target.proof.root.cwd)
                 == cwd]
        if len(exact) == 1:
            return exact[0]
        if any(root.root.session_id in uncertain
               and dispatch.normalise_root(root.root.cwd) == cwd
               for root in captured[1]):
            return refuse("holder-uncertain")
        # Published exact evidence cannot decay into the legacy cwd fallback.
        if had_exact_evidence or any(
                target.proof and dispatch.normalise_root(target.proof.root.cwd) == cwd
                for target in targets.values()):
            return refuse("exact-parent-mismatch")
        session_id = self._board_request_session(port)
        return session_id if session_id else refuse("legacy-unattributed")

    def _board_author_guard(self, port: int):
        """Hold a Codex registry/root generation across downstream board awaits."""
        entry = self._channels.get(port)
        if not entry or entry.get("host") != channel_server.HOST_CODEX:
            return None
        registry, captured = dict(entry), self._navigation_capture()
        def current():
            reason = ""
            if self._channels.get(port) is not entry or entry != registry:
                reason = "registry-changed"
            elif not self._navigation_capture_current(captured):
                reason = "roster-changed"
            if reason:
                logger.info("board attribution refused: stage=write reason=%s port=%d", reason, port)
            return not reason
        return current

    async def reveal_in_vscode(self, session_id: str) -> tuple[bool, str]:
        """Jump to a session in VS Code: raise its window and focus its terminal tab.

        Shared by the menu-bar 'Reveal in VS Code' row and the panel's spacebar
        (card_clicked). Resolves the session to its live Claude PID, then hands off to
        vscode_reveal (CLI raise + extension fan-out). Never raises — logs and returns
        (ok, detail); the surfaces are fire-and-forget."""
        try:
            async with asyncio.timeout(5.0):
                if session_id in self._codex_records:
                    terminal = self._codex_terminal_current(session_id)
                    if terminal is not None:
                        roots = codex_terminal.roots(self._codex_records.values())
                        targets = await codex_terminal.resolve(roots, recheck=True)
                        target = targets.get(session_id)
                        pid = (target.pid if target == terminal
                               and self._codex_terminal_current(session_id) == terminal
                               and roots == codex_terminal.roots(self._codex_records.values())
                               else None)
                    else:
                        captured = self._navigation_capture()
                        targets = await self._fresh_navigation_targets(captured)
                        target = targets.get(session_id)
                        pid = target.pid if target is not None else None
                else:
                    pid = await asyncio.get_running_loop().run_in_executor(
                        None, self._legacy_navigation_pid,
                        self._legacy_navigation_input(session_id))
                if pid is None:
                    return False, "no live PID for this session"
                if self._pty.owns(pid) is not None:
                    # Dark Army's own terminal: there is no editor window to
                    # raise. The panel draws it under the selected row, so
                    # Jump there is "select the row" and nothing more.
                    return True, "shown in Dark Army's own panel"
                return await vscode_reveal.reveal(pid)
        except Exception as exc:  # never let a reveal take down the caller
            logger.warning("reveal: failed for %s: %s", session_id[:12], exc)
            return False, f"reveal failed: {exc}"

    async def reveal_panel_for_terminal(
        self, shell_pid: int, tty: str,
    ) -> tuple[bool, str]:
        """Jump *back*: bring Dark Army's panel forward on the session in a terminal.

        `reveal_in_vscode`'s mirror. The extension can name the terminal it is
        looking at (a shell pid and a tty) but has no idea which session lives
        in it; the daemon is the only party that can answer that, because it
        holds the sessions and `_resolve_session_pid`'s identity guard.

        Both inputs arrive over the loopback API and are treated as untrusted
        *aim*: the worst a wrong pair achieves is the wrong row highlighted, so
        this always answers `(True, detail)` — a terminal Dark Army cannot place still
        opens the panel, with the reason in words rather than as a refusal the
        editor would have to render.
        """
        captured = self._navigation_capture()
        # Grok lives in `_session_states` when hooks have spoken, and only
        # in `_grok_records` before that. Either is a reverse-jump candidate;
        # Codex has its own matcher and stays out. `_agent_records` is Claude's
        # own roster: a session whose turn ended with no card is evicted from
        # `_session_states` after the staleness timeout, yet its row stays on
        # the panel from the roster — so the press on an idle terminal is
        # exactly the one that must still find it.
        seen: set[str] = set()
        legacy_ids: list[str] = []
        for sid in (list(self._session_states) + list(self._grok_records)
                    + list(self._agent_records)):
            if sid in self._codex_records or sid in seen:
                continue
            seen.add(sid)
            legacy_ids.append(sid)
        legacy_ids_t = tuple(legacy_ids)
        legacy_stamps = {
            sid: float((self._session_states.get(sid) or {}).get("last_event", 0) or 0)
            for sid in legacy_ids_t
        }
        legacy_inputs = tuple(self._legacy_navigation_input(sid) for sid in legacy_ids_t)

        def legacy_candidates():
            return [(facts[0], pid, legacy_stamps[facts[0]]) for facts in legacy_inputs
                    if (pid := self._legacy_navigation_pid(facts)) is not None]

        matched = None
        try:
            async with asyncio.timeout(5.0):
                # Candidate construction shares the matcher's deadline.
                loop = asyncio.get_running_loop()
                legacy = await loop.run_in_executor(None, legacy_candidates)
                targets = await self._fresh_navigation_targets(captured)
                legacy_match = await vscode_reveal.session_for_terminal(shell_pid, tty, legacy)
                codex_matches = set()
                normalized = codex_rollouts._normalise_tty(tty)
                ancestry_matches = set()
                for sid, target in targets.items():
                    if await vscode_reveal.session_for_terminal(
                            shell_pid, "", [(sid, target.pid, 0.0)]) == sid:
                        ancestry_matches.add(sid)
                for sid, target in targets.items():
                    if ancestry_matches and sid not in ancestry_matches:
                        continue
                    if normalized and target.tty != normalized:
                        continue
                    found = await vscode_reveal.session_for_terminal(
                        shell_pid, tty, [(sid, target.pid, 0.0)])
                    if found == sid:
                        codex_matches.add(sid)
                if len(codex_matches) == 1 and not legacy_match:
                    sid = next(iter(codex_matches))
                    fresh = await self._fresh_navigation_targets(captured)
                    if fresh.get(sid) == targets[sid] and self._navigation_capture_current(captured):
                        matched = sid
                elif not codex_matches and not ancestry_matches:
                    matched = legacy_match
        except asyncio.TimeoutError:
            logger.info("reveal_panel: matching timed out for pid %s", shell_pid)
        except Exception:
            logger.warning("reveal_panel: matching failed", exc_info=True)

        self._notify_observers("on_panel_reveal", matched or "")
        if matched:
            logger.info("reveal_panel: terminal %s → %s", shell_pid, matched[:12])
            return True, ""
        return True, "no session matched this terminal — opened the panel"

    async def _confirm_stop(self, session_id: str, pid: int) -> None:
        """Wait briefly for a SIGTERM'd session to die; escalate if it doesn't.

        Without this the row lingers until the next reconciler poll (up to 10s),
        which reads as the button having done nothing.

        And sometimes SIGTERM is not enough. A session wedged mid-turn — the case
        this escalation was written for had been "busy" with no activity for 311
        hours — never runs its shutdown path, so the signal changes nothing and a
        second click changes nothing twice. SIGKILL after ~6s, because the reason
        we preferred SIGTERM (let Claude Code flush its transcript) has already
        failed by then: a process that ignored the request for six seconds is not
        about to write the file. Stopping means stopping.

        Identity is re-verified before the kill for the same reason stop_session
        checks it before the terminate — the PID may have died and been recycled
        in those six seconds, and SIGKILL to a stranger is worse than SIGTERM to
        one."""
        for delay in (0.4, 0.8, 1.5, 3.0):
            await asyncio.sleep(delay)
            if not psutil.pid_exists(pid):
                self._forget_stopped(session_id)
                return

        logger.info("session %s did not exit within ~6s of SIGTERM — escalating",
                    session_id[:12])
        try:
            proc = psutil.Process(pid)
            if not pid_resolver.looks_like_session(
                proc.name(), " ".join(proc.cmdline() or []),
                self._session_provider(session_id),
            ):
                logger.warning(
                    "not escalating on %s: PID %d is no longer a session process",
                    session_id[:12], pid,
                )
                self._agents_poller.poll_soon()
                return
            proc.kill()
            logger.info("sent SIGKILL to PID %d (%s)", pid, session_id[:12])
        except psutil.NoSuchProcess:
            self._forget_stopped(session_id)
            return
        except (psutil.AccessDenied, OSError) as exc:
            logger.warning("could not SIGKILL PID %d: %s", pid, exc)
            self._agents_poller.poll_soon()
            return

        # SIGKILL is not caught, but reaping is not instantaneous either.
        for delay in (0.2, 0.5, 1.0):
            await asyncio.sleep(delay)
            if not psutil.pid_exists(pid):
                self._forget_stopped(session_id)
                return
        logger.warning("session %s survived SIGKILL (PID %d) — leaving the row",
                       session_id[:12], pid)
        self._agents_poller.poll_soon()

    def _forget_stopped(self, session_id: str) -> None:
        """Drop a stopped session from both sources and repaint. `_agent_records`
        too, not just the hook state: for a reconciler-only session that record is
        the *only* thing keeping the row alive, and it would otherwise survive
        until `claude agents --json` stopped listing it."""
        had_hook_state = session_id in self._session_states
        # Captured *before* the forget: `_forget_session` pops `_grok_records`
        # itself, so a read afterwards found nothing and the Grok tombstone
        # branch below was dead — a stopped roster-only Grok session simply
        # vanished instead of reaching Recently finished.
        rec = self._agent_records.get(session_id)
        grok = self._grok_records.get(session_id)
        self._forget_session(session_id, "stopped")
        # A reconciler-only session has no hook state for _forget_session to
        # tombstone, and popping the record below is what stops the poll diff from
        # noticing it left. Record it here or a stopped background agent vanishes.
        if not had_hook_state and rec is not None:
            self._record_finished(
                session_id,
                {"project": rec.project, "pid": rec.pid, "state": rec.activity},
                "stopped",
            )
        elif not had_hook_state and grok is not None:
            self._record_finished(
                session_id,
                {"project": grok.project, "pid": grok.pid, "state": "idle",
                 "provider": "grok"},
                "stopped",
            )
        self._agent_records.pop(session_id, None)
        self._grok_records.pop(session_id, None)
        self._blocked_since.pop(session_id, None)
        if had_hook_state:
            self._persist_sessions()
        self._agents_poller.poll_soon()
        self._notify_activity()
        self._schedule_agents_push()
        self._schedule_display_push()

    def _activity_counts(self) -> dict:
        """Bucket every known session into working / idle / attention, plus the
        total live subagent count.

        Single source of truth for all three surfaces that show the breakdown —
        the menu-bar strip, the expandable menu sections, and the simulator's HUD
        — so they can never disagree. The buckets are disjoint, so they sum to the
        number of *known* sessions, which since the reconciler landed is
        len(self._session_states) plus any background agents only Claude knows
        about."""
        bucket = {"running": "working", "sleeping": "idle", "waiting": "attention"}
        counts = {"working": 0, "idle": 0, "attention": 0, "subagents": 0}
        for s in self._session_states.values():
            counts["subagents"] += len(s.get("subagents", set()))
        counts["subagents"] += sum(
            len(rec.stats.agents)
            for rec in self._codex_records.values()
            if not rec.is_child and not self._closed_by_bob(rec.session_id)
        )
        for category in self._reconciled_categories().values():
            if category == "abandoned":
                continue      # deliberately uncounted: not a session any more
            counts[bucket[category]] += 1
        return counts

    def _notify_activity(self) -> None:
        """Push the per-category activity counts to every observer (menu bar,
        panel API). Dispatch is guarded per observer — see
        _notify_observers for why that matters on this path."""
        if not self._observers_implementing("on_activity_change"):
            return
        c = self._activity_counts()
        self._notify_observers(
            "on_activity_change", c["working"], c["idle"], c["attention"], c["subagents"]
        )

    def _session_capabilities(self, stub: dict) -> dict[str, bool]:
        """The controls this exact row may advertise; actions still re-check."""
        provider = stub.get("provider") or "claude"
        if provider == "codex":
            record = self._codex_records.get(stub.get("session_id", ""))
            identity = record.process_identity if record is not None else None
            if record is None or record.is_child or stub.get("alive") is False:
                return {
                    "can_stop": False,
                    "can_jump": False,
                    "can_hide": False,
                    "can_resume": False,
                }
            match_kind = identity.match_kind if identity is not None else ""
            return {
                "can_stop": match_kind == "explicit_resume",
                "can_jump": match_kind in ("explicit_resume", "unique_cwd") or bool(
                    self._navigation_proof_current(record.session_id)
                    or self._codex_terminal_current(record.session_id)),
                "can_hide": match_kind != "explicit_resume",
                "can_resume": False,
            }

        live_pid = bool(stub.get("pid")) and stub.get("alive") is not False
        # A lost tab has no editor to raise. A stale pid must not keep Jump.
        can_jump = (
            live_pid and stub.get("kind") != "background"
            and not stub.get("tab_gone")
        )
        return {
            "can_stop": live_pid,
            "can_jump": can_jump,
            "can_hide": False,
            "can_resume": provider == "claude",
        }

    def _collect_agent_stubs(self) -> list[dict]:
        """Snapshot the live session fields into plain dicts. Cheap and must run
        on the event-loop thread so it never races with _session_states mutation;
        the expensive transcript parsing then happens in _enrich_agent_stubs off
        the loop."""
        now = time.time()
        categories = self._reconciled_categories()
        self._prune_finished()
        # Metrics outlive their session otherwise: nothing else knows when a
        # statusline-reporting session is gone for good. Tombstoned sessions are
        # spared — the whole point of the finished section is that the cost of a
        # run survives the run, and this is the only copy of that number.
        for sid in list(self._session_metrics):
            if sid not in categories and sid not in self._finished:
                del self._session_metrics[sid]
        # The ring is pruned harder than the metrics beside it, and deliberately:
        # a tombstone keeps its final cost because that is what the finished
        # section is for, but a *rate* for a session that has stopped is not a
        # figure anyone should be shown.
        self._samples.keep_only(categories.keys())
        stubs: list[dict] = []
        seen: set[str] = set()

        def emit(stub: dict) -> None:
            # One run, one row. First source wins: hook → roster → Grok →
            # Codex → tombstone.
            sid = stub["session_id"]
            if sid in seen:
                return
            seen.add(sid)
            stubs.append(stub)

        for sid, st in list(self._session_states.items()):
            subs = st.get("subagents") or set()
            rec = self._agent_records.get(sid)
            emit({
                "session_id": sid,
                "project": st.get("project", "") or (rec.project if rec else ""),
                "state": st.get("state"),
                "subagents": len(subs),
                # Ids of the live subagents, so the enrich pass can label them
                # from the parent transcript. Sorted for stable menu ordering.
                "subagent_ids": sorted(subs),
                "pid": self._ensure_session_pid(sid, st),
                "current_tool": st.get("tool_name", ""),
                "ask_note": st.get("ask_note", ""),
                "idle_seconds": max(0.0, now - st.get("last_event", now)),
                "quiet_since": _quiet_stamp(st.get("last_event")),
                "kind": rec.kind if rec else "interactive",
                "agent_activity": rec.activity if rec else "",
                "cli_name": rec.name if rec else "",
                # Full path, unlike `project` (its basename): two repos can
                # share a basename, and the detail view has room to disambiguate.
                "cwd": (rec.cwd if rec is not None else "") or st.get("cwd") or "",
                "provider": st.get("provider") or (
                    "grok" if sid in self._grok_records else "claude"
                ),
                # The run-health counters, copied so the executor never
                # reads `_session_states`; absent on a restored state reads 0.
                **{key: int(st.get(key) or 0) for key in run_health.COUNTER_KEYS},
                # Survives onto the published row. Absent is not gone.
                "tab_gone": bool(st.get("tab_gone")),
                "_category": categories.get(sid, "sleeping"),
            })

        # Sessions Claude knows about and the hook stream does not: background
        # agents emit none of the terminal-shaped events the rest of the daemon
        # keys on, so without this they are invisible everywhere.
        for sid, rec in self._agent_records.items():
            if sid in self._session_states:
                continue
            # The third hookless source: `claude agents --json`. Filtered for
            # the same reason as the Codex and Grok rosters — a background
            # agent emits no hook stream to be turned away at the door.
            # Empty cwd is kept: the stub has not named a folder, and the
            # sweep at the end of this function keeps that case for the
            # same reason (a row whose folder is not resolved yet).
            if rec.cwd and not enrollment.root_enrolled(rec.cwd):
                continue
            emit({
                "session_id": sid,
                "project": rec.project,
                "state": rec.activity,
                "subagents": 0,
                "subagent_ids": [],
                "pid": rec.pid,
                "current_tool": "",
                # No hook events means no last_event to measure from. Age since
                # start is the honest proxy: for a background agent parked on
                # `blocked`, that IS how long it has gone unattended.
                "idle_seconds": max(0.0, now - rec.started_at) if rec.started_at else 0.0,
                "quiet_since": _quiet_stamp(rec.started_at),
                "idle_is_age": True,
                "started_at": rec.started_at,
                "kind": rec.kind,
                "agent_activity": rec.activity,
                "cli_name": rec.name,
                "cwd": rec.cwd,
                "provider": "claude",
                "_category": categories.get(sid, "sleeping"),
            })

        # Grok sessions the hook stream has not (yet) seen. Same shape as the
        # reconciler-only Claude rows above, sourced from a file rather than a fork.
        for sid, rec in self._grok_records.items():
            if sid in self._session_states:
                continue
            emit({
                "session_id": sid,
                "project": rec.project,
                "state": "working" if categories.get(sid) == "running" else "idle",
                "subagents": 0,
                "subagent_ids": [],
                "pid": rec.pid,
                "current_tool": "",
                "idle_seconds": max(0.0, time.time() - rec.opened_at) if rec.opened_at else 0.0,
                "quiet_since": _quiet_stamp(rec.opened_at),
                "idle_is_age": True,
                "started_at": rec.opened_at,
                "kind": "interactive",
                "agent_activity": "",
                "cli_name": "",
                "cwd": rec.cwd,
                "provider": "grok",
                "_category": categories.get(sid, "sleeping"),
            })

        # Codex sessions are namespaced because all three harnesses use opaque
        # UUID-like ids. The record already contains parsed stats, so enrichment
        # below does not try to resolve it as a Claude transcript.
        for sid, rec in self._codex_records.items():
            if rec.is_child or self._closed_by_bob(sid):
                continue
            emit({
                "session_id": sid,
                "project": rec.project,
                "state": "idle" if self._codex_left_open(rec) else rec.activity,
                "subagents": len(rec.stats.agents),
                "subagent_ids": sorted(rec.stats.agents),
                "pid": rec.pid,
                "current_tool": rec.current_tool,
                "idle_seconds": max(0.0, now - rec.last_event),
                "quiet_since": _quiet_stamp(rec.last_event),
                "started_at": rec.started_at,
                "kind": rec.kind,
                "agent_activity": rec.activity,
                "cli_name": rec.title,
                "cwd": rec.cwd,
                "provider": "codex",
                "_codex_stats": rec.stats,
                "_codex_metrics": rec.metrics,
                "review_reports": rec.review_reports,
                "review_reports_omitted": rec.review_reports_omitted,
                "_category": categories.get(sid, "sleeping"),
            })

        # Sessions that are over. Neither source reports them any more — that is
        # what being over means — so they come from the tombstone store, and their
        # duration counts from when work stopped rather than from removal.
        for sid, record in self._finished.items():
            if (sid in self._session_states or sid in self._agent_records
                    or sid in self._grok_records
                    or (sid in self._codex_records and not self._closed_by_bob(sid))):
                continue        # it came back; the live stub above wins
            emit({
                **record["stub"],
                "idle_seconds": max(0.0, now - record["finished_at"]),
                "quiet_since": _quiet_stamp(record["finished_at"]),
                "finished_at": record["finished_at"],
                "end_reason": record["end_reason"],
                "alive": False,
                "_category": "finished",
            })
        # One sweep, for anything the three filters above did not reach — a
        # tombstone of a row that was enrolled when it finished, say.
        #
        # **The asymmetry is deliberate.** A stub whose `cwd` is *empty* is
        # kept: it came in through the hook stream, so it already passed the
        # key gate at the door, and dropping it would delete every row whose
        # folder has not been resolved yet. Only a stub that names a folder and
        # names an unenrolled one is dropped.
        stubs = [st for st in stubs
                 if not st.get("cwd") or enrollment.root_enrolled(st["cwd"])]
        for stub in stubs:
            stub.update(self._session_capabilities(stub))
            # The moment the row went quiet, as one absolute stamp beside the
            # age: `idle_seconds` is an age as of *this* build, and a client
            # that subtracts it from a later clock (the frame's `generated_at`,
            # stamped at serve time) dates the row a tick late — differently
            # from the daemon's own reading, so the phone's Live Activity and
            # the Mac's first push disagreed on the clock by more than the
            # drift `live_activity.same` forgives. Each source above stamps
            # it from its own clock (`_quiet_stamp`: the hook stream's
            # `last_event`, the roster's `started_at`, Grok's `opened_at`,
            # Codex's `last_event`, a tombstone's `finished_at`) and writes
            # 0.0 where it has none — never `now - idle`, which stamped
            # `now` on a stampless row and moved every frame. The key is a
            # clock field (`api_server._CLOCK_FIELDS`, beside `last_event`,
            # which it is a rounding of): a hook row's stamp moves on every
            # event, so as news it made two same-tool events on a working
            # row differ on this key alone — one SSE frame per tool call and
            # a phone digest that never settled. A row's move to waiting is
            # news through `state`, and every full read carries the value.
            # A new key with a default on read (the phone's
            # `Agent.quietSince`, 0 from an older daemon; 0 is undated).
            stub.setdefault("quiet_since", 0.0)
        return stubs

    def _session_started_at(self, sid: str, stub: dict, stats) -> float:
        """When this session first started, as a number that never moves.

        Three sources, in order of how well they survive a restart. A
        reconciler-only row (a background agent, a Grok session) already carries
        the roster's own `started_at`. Everything else prefers the **first
        timestamp in the transcript**, which is the session's real birth and is
        re-derivable from disk, so a daemon restart does not renumber the fleet.
        Only a session with no transcript yet — a brand-new one, in its first
        seconds — falls back to a memo of when this daemon first saw it, seeded
        from its last event rather than from `now` so a session recovered from
        `sessions.json` does not claim to have started at the restart.
        """
        known = stub.get("started_at")
        if isinstance(known, (int, float)) and known:
            return float(known)
        first = getattr(stats, "first_ts", None)
        if first is not None:
            try:
                return first.timestamp()
            except (AttributeError, ValueError, OSError):
                pass
        memo = self.__dict__.setdefault("_first_seen", {})
        finished = stub.get("finished_at")
        if isinstance(finished, (int, float)) and finished:
            # A tombstone with no recorded start: its finish is the one fixed
            # stamp it has. Seeding from `now - idle` put the build's own
            # clock on the row, a few milliseconds past the finish.
            return memo.setdefault(sid, float(finished))
        return memo.setdefault(
            sid, time.time() - max(0.0, float(stub.get("idle_seconds") or 0.0))
        )

    def _project_facts_snapshot(self, names: set[str]) -> list[dict]:
        """Per-project standing facts for the panel header. Executor-only.

        Refreshed on PROJECT_FACTS_INTERVAL or when the live-project set
        changes — a new tab must not wait a minute for its numbers.
        last_active is minute-rounded so the quiet limiter never sees the
        clock as news. A raising query keeps the last cache: this is
        decoration on the seven-flow snapshot path.
        """
        cache = self.__dict__.setdefault("_project_facts", [])
        if self._history is None:
            return cache
        at = self.__dict__.setdefault("_project_facts_at", 0.0)
        prev = self.__dict__.setdefault("_project_facts_names", set())
        if (time.monotonic() - at < PROJECT_FACTS_INTERVAL
                and names == prev):
            return cache
        try:
            rows = self._history.project_cwd_facts()
        except Exception:
            logger.debug("project facts refresh failed", exc_info=True)
            return cache
        folded: dict[str, dict] = {}
        for row in rows:
            stored = row["project"] or ""
            cwd = row["cwd"] or ""
            label = (workspace.project_label(cwd, None, stored) if cwd
                     else stored)
            if not label or label not in names:
                continue
            sessions = int(row["sessions"] or 0)
            last_active = float(row["last_active"] or 0)
            agg = folded.get(label)
            if agg is None:
                folded[label] = {"sessions": sessions,
                                 "last_active": last_active}
            else:
                agg["sessions"] += sessions
                if last_active > agg["last_active"]:
                    agg["last_active"] = last_active
        facts = [
            {
                "project": label,
                "sessions": int(agg["sessions"]),
                "last_active": int((agg["last_active"] or 0) // 60) * 60,
            }
            for label, agg in folded.items()
        ]
        self._project_facts = facts
        self._project_facts_at = time.monotonic()
        self._project_facts_names = set(names)
        return facts

    def _power_source_snapshot(self) -> dict:
        """The host Mac's power source, re-read on `POWER_SOURCE_INTERVAL`.
        Executor-only: `power_source.read()` spawns `pmset`.

        The memo is **rebound, never mutated** — `power_snapshot()` copies
        it on the loop while this may be running. A raising read keeps the
        last reading rather than blanking the phone's line.

        Aged on **both** clocks: `time.monotonic()` stops while the Mac
        sleeps, so a pre-sleep reading would pass for fresh for a minute
        after the lid opens; the wall clock (`_power_wall`) catches that,
        and a wall clock that went backwards re-reads too."""
        cache = self.__dict__.setdefault("_power", power_source.unavailable())
        at = self.__dict__.setdefault("_power_at", None)
        wall_at = self.__dict__.setdefault("_power_wall", None)
        now = time.monotonic()
        wall = time.time()
        if (at is not None and wall_at is not None
                and now - at < POWER_SOURCE_INTERVAL
                and 0 <= wall - wall_at < POWER_SOURCE_INTERVAL):
            return cache
        try:
            reading = power_source.read()
        except Exception:
            logger.debug("power source read failed", exc_info=True)
            return cache
        self._power = dict(reading)
        self._power_at = now
        self._power_wall = wall
        return self._power

    def power_snapshot(self) -> dict:
        """The `power` section of `/api/state`: the host Mac's battery as
        `power_source.parse` states it. On the loop; a copy of the memo the
        executor rebinds, `{"available": False}` before the first read."""
        return dict(self.__dict__.get("_power") or power_source.unavailable())

    def _registry_cached(self) -> dict:
        """`session_registry.read_registry()`, reused for up to
        `FULL_PUSH_INTERVAL_SECONDS`. Executor only (`_enrich_agent_stubs`);
        the full tier of `_push_agents_snapshot` clears the memo on the loop
        before its enrich hop, so a structural change always re-reads."""
        memo = getattr(self, "_registry_memo", None)
        now = time.monotonic()
        if memo is not None and now - memo[0] < self.FULL_PUSH_INTERVAL_SECONDS:
            return memo[1]
        registry = session_registry.read_registry()
        self._registry_memo = (now, registry)
        return registry

    def _enrich_agent_stubs(self, stubs: list[dict]) -> dict:
        """Resolve + parse each stub's transcript and group by category. Blocking
        file I/O — run off the event loop (executor). Only one enrichment runs at
        a time (see _schedule_agents_push), so the stats cache isn't contended."""
        from . import session_stats as ss
        from . import grok_billing
        from . import grok_chat
        from . import grok_events
        from . import grok_usage

        cache = self.__dict__.setdefault("_stats_cache", ss.StatsCache())
        grok_usage_cache = self.__dict__.setdefault(
            "_grok_usage_cache", grok_usage.UsageCache())
        grok_events_cache = self.__dict__.setdefault(
            "_grok_events_cache", grok_events.EventsCache())
        grok_chat_cache = self.__dict__.setdefault(
            "_grok_chat_cache", grok_chat.ChatCache())
        # One billing fetch per snapshot, not per Grok row. The module caches
        # for 60s; a failed refresh keeps the last good reading.
        grok_budget = grok_billing.get_snapshot()
        # "abandoned" is a fourth bucket the menu deliberately does not render —
        # it iterates the three live categories by name — so long-dead background
        # agents stay out of the dropdown while remaining in the panel.
        # Buckets, and *only* buckets: several consumers walk this dict as
        # `category -> [row, ...]` (`signals.evaluate`, `alerting`), so a
        # non-list key here is an AttributeError in each of them. Enrolment is
        # published at the top level of `ApiServer.state()` instead, from
        # `enrollment_snapshot()` — one function, so the surfaces cannot
        # disagree about it.
        out: dict = {"running": [], "sleeping": [], "waiting": [],
                     "abandoned": [], "finished": []}

        # Read once per enrichment pass, beside the alert path's own read of
        # the same function further down: a session blocked on a permission
        # ask cannot have its terminal closed (`close_session_terminal`
        # refuses it), so a `can_close` that ignored prompts drew a button
        # the daemon would refuse. One source of truth, no second walk.
        open_prompt_sids = set(self._prompts_by_session())
        # And once, beside it, the sessions working a live card. The message
        # flag tests membership here *first*, so `can_send_text`'s `ps` is never
        # asked for a row that is not working a card — the cost gate that
        # `can_type`'s stopped-category test buys the same way.
        carded_sids = self._carded_live_sessions()
        # And, from the same already-published board, the card title each
        # session is working under — the fallback for a row that would
        # otherwise read `UNNAMED_SESSION`. One join, on the daemon, because
        # neither client has a session-to-card join and must not grow one.
        card_titles = self._card_titles_by_session()
        # And, from the same board, every card's own title by id — what an
        # `origin` stamp needs to turn "card-start|<id>|<stage>" into a
        # sentence. Separate from the join above on purpose: that one is the
        # unnamed-row fallback and its presence is a judgment; this one is a
        # lookup table.
        card_title_by_id = self._card_title_by_id()
        card_area_by_id = self._card_area_by_id()

        # Claude Code's own view of what is running. One read per snapshot, not
        # one per session — it is a directory of small files, but this path runs
        # every few seconds and the cost of a habit is what it does all day.
        # Memoised across light-tier pushes; the full tier clears it.
        registry = self._registry_cached()

        # Parse first, assign nicknames second. Assignment needs each session's
        # working directory (the transcript is the most reliable source of it)
        # and needs to know which names are already spoken for, so it cannot
        # happen inside the same loop that is still discovering both.
        parsed = []
        for stub in stubs:
            sid = stub["session_id"]
            provider = stub.get("provider") or "claude"
            if provider == "grok":
                stats, grok_metrics = grok_roster.enrich(
                    sid, stub.get("cwd") or "",
                    usage_cache=grok_usage_cache,
                    events_cache=grok_events_cache,
                    chat_cache=grok_chat_cache,
                    billing=grok_budget,
                )
                grok_title = grok_roster.title_of(
                    grok_roster.read_summary(sid, stub.get("cwd") or ""))
                if grok_title and not stub.get("cli_name"):
                    stub = {**stub, "cli_name": grok_title}
                if grok_metrics:
                    stub = {**stub, "_grok_metrics": grok_metrics}
                live_subs = grok_roster.live_subagent_ids(sid, stub.get("cwd") or "")
                if live_subs is not None:
                    stub = {
                        **stub,
                        "subagents": len(live_subs),
                        "subagent_ids": live_subs,
                        "_subs_reconciled": True,
                    }
                self._record_grok_history(sid, grok_metrics, grok_title)
                parsed.append((stub, "", stats))
            elif provider == "codex":
                parsed.append((stub, "", stub.get("_codex_stats") or ss.SessionStats()))
            else:
                transcript = ss.resolve_transcript(sid)
                stats = cache.get(transcript)
                parsed.append((stub, transcript, stats))

        nicknames = self._assign_nicknames(parsed)
        # What this snapshot puts on screen, for the event log's finish line
        # to read after the row is gone. Built here, assigned whole below.
        names_shown: dict[str, str] = {}
        starts_shown: dict[str, float] = {}

        for stub, transcript, stats in parsed:
            sid = stub["session_id"]
            reg = registry.get(sid)
            # Mission Control is always called Mission Control. It is one
            # standing chat, so a title drawn from its latest question (or
            # from the first message after a `/clear`) would rename it every
            # few turns and hide it among the fleet. The origin stamp — live
            # state first, then the tombstone's copy — is the same test the
            # origin line below uses, and it also spares the title helper a
            # call it would only have thrown away.
            if origin.parse(
                    (self._session_states.get(sid) or {}).get("origin")
                    or stub.get("origin") or "").get("by") == "mission":
                name = mission.NAME
            else:
                name = self._session_name(sid, stub, transcript, reg)
            entry = {k: v for k, v in stub.items()
                     if k not in ("_category", "_codex_stats",
                                  "_codex_metrics", "origin")}
            entry["name"] = name
            # The identity, as opposed to the description above. Held separately
            # rather than folded into `name` so a surface can show both — the
            # panel has room for "Vex · Add options trading", the menu bar
            # does not.
            entry["nickname"] = nicknames.get(sid, "")
            # A session Dark Army started for a card, that earned no name of its
            # own, wears the card's title. The key is *absent* rather than
            # empty wherever that is not so: its presence is the already-made
            # judgment "this row is unnamed and this card names it", so no
            # client repeats it and neither learns the string `New session`.
            # The row's own name is deliberately untouched — folding it in
            # would rewrite the VS Code tab and the event log's `who`.
            if name == UNNAMED_SESSION and card_titles.get(sid):
                entry["card_title"] = card_titles[sid]
            # The same judgment as a bare bool, for the one client rule that
            # needs it without a card: the phone's Live Activity `work` line
            # is the card's title, else the name — and never the placeholder
            # (`_compose_push_work`'s rule). Present and `True` only where the
            # row is unnamed; absent otherwise, so an older phone reads past
            # it and a newer phone against an older daemon decodes `false`
            # and draws the name it always did.
            if name == UNNAMED_SESSION:
                entry["unnamed"] = True
            # Where this session came from, if it says so. An assistant a
            # person started by hand carries no stamp and gets **none** of
            # these three keys, which is what makes the line worth reading:
            # it appears exactly where you did not press the button. The
            # sentence is composed here and drawn verbatim by both clients,
            # `queue_reason`'s rule — two surfaces each re-deriving a
            # sentence is two surfaces that can disagree about what happened.
            #
            # Two sources, in this order: the live session state, then the
            # tombstone's own copy (`_record_finished`). `_forget_session`
            # pops `_session_states`, so the second is what keeps a *Recently
            # finished* row answering the question its live row answered.
            stamped = origin.parse(
                (self._session_states.get(sid) or {}).get("origin")
                or stub.get("origin") or "")
            origin_title = ""
            if stamped:
                origin_title = card_title_by_id.get(stamped["card_id"], "")
                entry["origin_by"] = stamped["by"]
                entry["origin_card"] = stamped["card_id"]
                entry["origin_line"] = origin.sentence(
                    stamped["by"], origin_title, stamped["stage"])
            if stamped and stamped["by"] == "card-start":
                slug = card_area_by_id.get(stamped["card_id"], "")
                if slug:
                    entry["area"] = slug
                    entry["area_line"] = areas.lead_line(entry.get("nickname", ""), slug)
            entry["branch"] = stats.git_branch
            # What the agent is asking, when it is stopped on an
            # `AskUserQuestion`. The channel cannot answer this one — a pushed
            # event queues behind the very dialog it would answer — but the
            # keystroke route can: `answer_question` types digit-then-Enter
            # through `session_io.send_text`, in front of the dialog rather
            # than behind it, so the options here are button labels wherever
            # `can_type` is true and caption everywhere else. Either way the
            # text is worth carrying: "waiting for you" and "waiting for you,
            # about whether to use Postgres" are different amounts of news, and
            # only one of them tells you whether to get up. Empty for every
            # session that is not stopped on a question, which is nearly all.
            # Two sources, transcript first. It is the better one — it carries
            # the real tool_use id, so an answer aimed at a question the
            # terminal has already dealt with can miss — but it is empty for
            # the whole time the dialog is up, because Claude Code writes the
            # asking turn only alongside the answer. The hook's copy fills
            # exactly that gap and is superseded the moment the transcript
            # catches up.
            # The whole dialog beside the flat first question, from one
            # source at a time — pairing a transcript's flat question with a
            # hook-held list could describe two different dialogs. The flat
            # key is never renamed and never becomes a list: an older surface
            # keeps decoding it exactly as before and ignores the sibling; a
            # newer one wraps a non-empty flat into a one-element list when
            # the sender has none.
            capture = getattr(self, "_decisions", None)
            results = getattr(stats, "question_results", [])
            if capture:
                for result in results:
                    capture.submit("observed_question_result", sid, **result)
            if stats.question:
                question = stats.question
                questions = list(stats.questions)
            else:
                pending = self._pending_questions.get(sid, {})
                if any(result["source_id"] == pending.get("id") for result in results):
                    pending = {}
                question = {k: v for k, v in pending.items()
                            if k != "questions"}
                questions = list(pending.get("questions") or [])
            if not questions and question:
                questions = [dict(question, index=0)]
            entry["question"] = question
            entry["questions"] = questions
            # And the freshest reading, kept for `answer_question` and
            # `answer_questions` to check a verdict against — the daemon
            # caches no snapshot, and an action handler must not re-parse a
            # transcript to find out what it is answering. One whole-value
            # write per key, the `_frontmost_pids` pattern.
            if question:
                self._questions[sid] = dict(question, questions=questions)
            else:
                self._questions.pop(sid, None)
            # Same snapshot as the question: a hookless Grok row would
            # otherwise stay `running` (can_type false, Needs you empty)
            # while the interview is already on the entry.
            if (stub.get("provider") == "grok" and question
                    and stub.get("_category") in ("running", "sleeping",
                                                  "waiting")):
                stub["_category"] = "waiting"
                entry["state"] = "waiting"
            # And for the far commoner wait that has no tool call behind it —
            # a turn that just ended with "accept?" — the agent's closing words.
            entry["last_text"] = stats.last_text
            # The agent's own one-sentence version of them, when it left one
            # behind the `bob-tldr` marker (asked for by the SessionStart hook,
            # parsed by session_stats). The caption a surface should prefer;
            # the raw tail above stays what a chevron expands to.
            entry["last_summary"] = stats.last_summary
            # The transcript's clocks for those markers and the person's last
            # prompt (epoch seconds, 0.0 where unknown): `alerts.offered_reply`
            # reads offered choices as this turn's only when written at or
            # after the prompt. Not clock fields for `api_server`: they move
            # only when the transcript does, which is news anyway.
            entry["marker_at"] = float(stats.marker_at or 0.0)
            entry["prompt_at"] = float(stats.prompt_at or 0.0)
            # And the last completion report it wrote, kept across the
            # follow-up chatter that would otherwise be all a surface had.
            # Claude slices `## Work done` from the transcript; Grok lifts
            # the same heading from chat_history. Empty when nothing has
            # finished yet; cleared by the person's next prompt.
            entry["last_report"] = stats.last_report
            # The same report split into its labelled parts, once, here —
            # the row, the banner, the inbox and both clients' details draw
            # this one shape and none re-parses the text (`work_report.py`;
            # it structures what is drawn and decides no state). Absent
            # where there is no report.
            if stats.last_report:
                entry["work_report"] = work_report.parse(stats.last_report)
            # The finished list's own word — "done" only where a report
            # says so (`session_stats.finish_word`). Composed here, after
            # the report is known, and drawn verbatim by both clients; an
            # older client without the key keeps saying "done".
            if stub.get("_category") == "finished":
                entry["finish_word"] = ss.finish_word(
                    stub.get("state"), stub.get("end_reason"),
                    stats.last_report)
            # A finished Grok with a report is nobody waiting: Stop cards
            # otherwise bank it under waiting. A live interview wins.
            # Grok often never fires Stop, so tool_name and helper counts
            # stick after the spoken turn ends — requiring them empty hid
            # Acknowledge & close on every finished Grok, including /ship
            # plan-mode which writes close-out: and never ## Work done.
            # The latest spoken turn is the signal: Work done, or the
            # ship tidy line. Later chatter without those is still a
            # running turn. StopFailure stays waiting.
            # Snapshot bucket only here: the executor must never touch
            # `_session_states` or pop `_active_notifications`. Tag the
            # entry so `_apply_grok_finished_demote` can idle hook state
            # and drop the leftover card on the loop.
            category = stub.get("_category")
            last_text = entry.get("last_text") or ""
            finished_running = (
                category == "running"
                and ("Work done" in last_text or "close-out:" in last_text)
            )
            if (stub.get("provider") == "grok"
                    and (entry["last_report"] or "close-out:" in last_text)
                    and not question
                    and sid not in open_prompt_sids
                    and (category == "waiting" or finished_running)):
                card = self._active_notifications.get(sid) or {}
                st = self._session_states.get(sid)
                leftover_error = (
                    card.get("hook") == "StopFailure"
                    or (st is not None and st.get("state") == "error")
                )
                if not leftover_error:
                    stub["_category"] = "sleeping"
                    entry["state"] = "idle"
                    entry["current_tool"] = ""
                    entry["_grok_finished_demote"] = True
                    entry["_grok_finished_seen_event"] = (
                        (st.get("last_event") if st is not None else 0) or 0
                    )
            # And the answers it offered, if it offered any (`bob-actions`).
            # A surface may draw these as buttons; each one is sent back
            # verbatim as a reply. Empty means the agent proposed nothing to
            # decide, and a surface must then offer a field rather than invent
            # a verdict for it — the panel drew "Accept" on every stopped row
            # before this existed, including rows that had asked nothing.
            entry["reply_options"] = list(stats.last_actions or [])
            # What another agent would have to type to reach this one, and
            # whether it could. Dark Army cannot send the message itself — see
            # session_registry — but an address you cannot see is one you cannot
            # use, and an unreachable session is otherwise found by trying.
            entry["address"] = reg.name if reg else ""
            entry["addressable"] = bool(reg and reg.addressable)
            if stub.get("alive") is False:
                entry["address"] = ""
                entry["addressable"] = False
                entry["retained_address"] = stub.get("retained_address", "")
                entry["retained_address_source"] = stub.get("retained_address_source", "")
            # Whether Dark Army can *type* at it — a different reach from the
            # channel below, and the two do not imply each other: the channel carries
            # prose to the model, `sendText` carries keystrokes to the client,
            # and only the second can run a slash command. This is what gates
            # the panel's AskUserQuestion option buttons. Asked only for
            # stopped rows, since `can_send_text` shells out to `ps` for a pid
            # it has not seen in five seconds and a running fleet would pay
            # that every snapshot.
            entry["interaction_note"] = ""
            codex_reply = False
            if stub.get("provider") == "codex":
                proof, note = self._codex_reply_candidate(sid)
                codex_reply = proof is not None and stub.get("alive") is not False
                entry["interaction_note"] = note
            entry["can_type"] = (
                stub.get("provider") != "codex"
                and stub.get("_category") in ("waiting", "sleeping")
                and session_io.can_send_text(entry.get("pid"))
            )
            # Whether Dark Army can put a line in front of this session by *some*
            # route — `channel` is the reply box's gate on both surfaces and
            # keeps that meaning — and `reply_via` says which route the next
            # reply takes: `"typed"` (`_reply_by_typing`, first when the
            # preference is on), `"channel"` (Claude via the channel, Grok
            # via a resident leader client), or `""` for none. Published so
            # a surface can decide whether to offer a reply box at all and
            # say where the words go, rather than working either out itself:
            # a push to a session Dark Army cannot reach is dropped in silence, so
            # an affordance drawn on one of those is a button that does
            # nothing and cannot say so. The typed route mirrors
            # `_reply_by_typing`'s dialog refusals here so the box is absent
            # rather than drawn and turned down; `can_type` goes first
            # because it already carries the stopped-category gate and the
            # one shared `ps`. `_session_reachable` itself is not widened —
            # `_decide_permission_ask`'s live-channel refusal reads it.
            channel_ok = stub.get("provider") != "codex" and self._session_reachable(sid)
            typed_ok = (
                bool(self.typed_reply_enabled)
                and bool(entry["can_type"])
                and sid not in open_prompt_sids
                and not self._questions.get(sid)
            )
            typed_ok = typed_ok or codex_reply
            entry["reply_via"] = ("codex" if codex_reply and isinstance(proof, codex_input.Proof)
                                  else "typed" if typed_ok
                                  else ("channel" if channel_ok else ""))
            entry["channel"] = bool(channel_ok or typed_ok)
            # Whether Dark Army can dispose this session's VS Code terminal tab.
            # Deliberately not `can_type` widened: `can_type` is about
            # keystrokes into an input line and stays false for Codex. This
            # flag is the wrap-up button's reach, and Codex's discriminator
            # is `can_stop` read off the stub (`match_kind ==
            # "explicit_resume"`) rather than re-derived. The stopped-category
            # gate is `can_type`'s cost argument verbatim — one shared `ps`
            # via `_in_vscode`, not a second one.
            #
            # And never while an open permission prompt is blocking the
            # session: `close_session_terminal` refuses that press, so the
            # button was a lie the phone drew and the daemon then turned down.
            # Whether this session runs on a terminal Dark Army itself owns — the
            # panel draws that terminal under the row instead of only the
            # stdout pane, and the phone can watch it. Asked only while any
            # pty is open, so the ordinary fleet costs no `ps` walk here.
            # The stub goes along so an unnamed terminal is named only after
            # a pipeline main agent (`_bindable_candidate`), never after a
            # background agent that merely descends from the same child.
            own_terminal = (self._pty_handle_for(sid, stub)
                            if len(self._pty) else None)
            entry["own_terminal"] = own_terminal is not None
            entry["can_close"] = (
                stub.get("_category") in ("waiting", "sleeping")
                and sid not in open_prompt_sids
                and stub.get("kind") != "background"
                and stub.get("alive") is not False
                and (stub.get("can_stop") is True
                     if (stub.get("provider") or "claude") == "codex" else True)
                # A Dark Army-owned terminal is closable by its session name for as
                # long as Dark Army holds it — an *exited* one included, whose pid
                # `ptyhost.owns` no longer answers for. `session_io` is the
                # editor path and is asked only for everything else.
                and (own_terminal is not None
                     or session_io.can_close_terminal(entry.get("pid")))
            )
            if ((stub.get("provider") or "claude") == "codex"
                    and stub.get("can_stop") is not True
                    and stub.get("_category") in ("waiting", "sleeping")
                    and stub.get("kind") != "background" and stub.get("alive") is not False
                    and (proof := self._codex_human_close_candidate(sid)) is not None):
                entry["can_close"] = vscode_reveal.can_close_native_terminal(proof.pid, proof.root.cwd)
            # And whether a line may be typed into it: the `terminal_input`
            # verb's reach, mirroring its refusals so the box is absent
            # rather than drawn and turned down. No open permission prompt
            # (`open_prompt_sids`, the same set `can_close` reads — a safety
            # dialog answered from a phone is a decision that belongs in
            # front of the machine), a live row, not background, and a
            # process still reading its pty.
            entry["can_terminal_input"] = bool(
                own_terminal is not None
                and sid not in open_prompt_sids
                and stub.get("kind") != "background"
                and stub.get("alive") is not False
                and not self._pty_exited(own_terminal)
            )
            # The words are `tab_gone`. These three flags are what hides
            # Jump and the hosted terminal; the pty binding is untouched,
            # and can_type / channel / can_close stay as they were.
            entry["tab_gone"] = bool(stub.get("tab_gone"))
            if entry["tab_gone"]:
                entry["can_jump"] = False
                entry["own_terminal"] = False
                entry["can_terminal_input"] = False
            # The Low priority button's reach. `can_type` goes first: it
            # already carries the stopped-category gate and the one shared
            # `ps`, so the `ps` is never asked twice and never for a running
            # row. The rest mirrors `low_priority_session`'s own refusals so
            # the button is absent rather than refused. Reading
            # `_active_notifications` here is the same standing race
            # `_prompts_by_session` documents.
            card = self._active_notifications.get(sid) or {}
            entry["can_low_priority"] = (
                entry["can_type"]
                and (stub.get("provider") or "claude") == "claude"
                and stub.get("kind") != "background"
                and sid not in open_prompt_sids
                and card.get("hook") == "StopFailure"
                and card.get("error_kind") == "rate_limit"
                and not self._low_priority_recent(sid)
            )
            # Whether Dark Army could type a person's own message at this session on
            # behalf of the card it is working — the Send message button's
            # reach, mirroring `message_card`'s refusals so the button is
            # absent rather than drawn and turned down.
            #
            # `can_type` is deliberately **not** a term. It carries a
            # stopped-category gate (`waiting`/`sleeping`) for a cost reason,
            # and this feature exists for a session that is *running*. The
            # cost argument still holds: the membership test comes first, so
            # `can_send_text` is asked only for card-bound rows — at most the
            # sum of the projects' `parallel_limit`s, each capped at 4 — and
            # `_can_send_cache`'s 5s TTL absorbs the repeats.
            #
            # The prompt gate is the safety half, not tidiness: `send_text`
            # ends in a newline that an open permission dialog reads as
            # confirming the highlighted choice, so a button drawn here would
            # approve a tool call nobody read.
            #
            # The question gate is its twin and is *not* covered by the first:
            # an `AskUserQuestion` dialog is deliberately absent from
            # `_prompts_by_session()` (the hook broker refuses to hold one), and
            # it owns the input line just as hard — Dark Army's own answer burst
            # commits a choice by exactly this route. `entry["question"]` is
            # already computed above, so it costs nothing and stays *after* the
            # membership test and *before* `can_send_text`: the `ps` cost gate
            # is untouched.
            entry["can_message"] = (
                sid in carded_sids
                and (stub.get("provider") or "claude") != "codex"
                and stub.get("kind") != "background"
                and stub.get("alive") is not False
                and sid not in open_prompt_sids
                and not entry.get("question")
                and session_io.can_send_text(entry.get("pid"))
            )
            # When this session first started — a fixed point, unlike every other
            # number on the row. It is what a surface orders by when it wants an
            # order that stays put: `idle_seconds` moves on every tool call, so a
            # list sorted by it reshuffles several times a minute under the hand
            # of whoever is reading it.
            entry["started_at"] = self._session_started_at(sid, stub, stats)
            entry["stats"] = ss.stats_to_dict(stats)
            # The three run-health counters ride the row beside `stats`,
            # from the stub's copy alone; a row with no hook stream (a
            # roster stub, Grok, Codex, a tombstone) publishes 0.
            for key in run_health.COUNTER_KEYS:
                entry[key] = int(stub.get(key) or 0)
            # Cost, context-window pressure and rate-limit budget — none of which
            # the transcript carries. Absent until the session's first statusline
            # tick, so consumers must treat {} as "not reported yet", not "zero".
            # Grok has no statusline; signals.json fills the same slot.
            metrics = dict(self._session_metrics.get(sid, {}))
            grok_metrics = stub.get("_grok_metrics") or {}
            for key, value in grok_metrics.items():
                metrics.setdefault(key, value)
            codex_metrics = stub.get("_codex_metrics") or {}
            for key, value in codex_metrics.items():
                metrics.setdefault(key, value)
            entry["metrics"] = metrics
            # The project, resolved from the VS Code workspace rather than left
            # as the `basename(cwd)` the stub arrived with. Here rather than in
            # `_collect_agent_stubs` because this is the one place every kind of
            # row passes through — hook-tracked, reconciler-only, Grok and
            # tombstone alike — and because the ladder needs `metrics`, which is
            # assembled two lines up. `entry["cwd"]` is the full path for exactly
            # this reason: two repos can share a basename.
            # Backstop for a row whose state predates the hook-carried cwd (or
            # was restored from an old sessions.json): the transcript recorded
            # the folder too. Written onto the stub copy only — the executor
            # must never touch `_session_states`.
            if not entry.get("cwd"):
                from_stats = getattr(stats, "cwd", "") or ""
                if from_stats:
                    entry["cwd"] = from_stats
            entry["project"] = workspace.project_label(
                entry.get("cwd", ""), metrics, entry.get("project", "")
            )
            # What those numbers are *doing*. Computed here rather than in
            # signals.py so the rules stay pure functions of one entry — and
            # published rather than kept private, because "context 62%, +3%/min"
            # is worth reading on the row whether or not any rule fires on it.
            entry["trend"] = self._samples.trend(sid, time.time())
            entry["provider"] = stub.get("provider") or "claude"
            # Who this session has messaged, in the address vocabulary the
            # sender used. Resolved into a fleet-level mesh below, once every
            # session's own address is known.
            entry["sent_to"] = dict(getattr(stats, "sent_to", {}) or {})
            entry["sent_to_partial"] = bool(getattr(stats, "sent_to_partial", False))

            # Each session carries its own live subagents, so the menu can render
            # them indented under their parent — visible without hovering, and
            # without every row having to repeat whose work it is.
            entry["subagent_rows"] = self._subagent_rows(
                transcript, stats, stub.get("subagent_ids") or [],
                card_title=origin_title,
            )
            out[stub["_category"]].append(entry)
            names_shown[sid] = name
            if isinstance(entry.get("started_at"), (int, float)):
                starts_shown[sid] = float(entry["started_at"])
            # The start line is written on the row's **first live appearance**,
            # not at `session_start`: the nickname is assigned just above and
            # the project resolved just below, and a line written at the hook
            # would read "an agent started" for every row. Tombstones and
            # abandoned agents are not starts. Running on the executor, so
            # `_log_event` appends directly.
            if (stub.get("_category") in ("waiting", "running", "sleeping")
                    and sid not in self._logged_starts
                    and not self._start_already_on_record(sid, entry)):
                self._logged_starts.add(sid)
                self._log_session_event(
                    sid, "session_start",
                    project=str(entry.get("project") or ""), title=name,
                    provider=str(entry.get("provider") or ""),
                    session_kind=str(entry.get("kind") or ""))
        self._names_shown = names_shown
        self._starts_shown = starts_shown

        # The memo below is only useful for sessions that are still around.
        self._first_seen = {
            sid: t for sid, t in getattr(self, "_first_seen", {}).items()
            if sid in {stub["session_id"] for stub, _t, _s in parsed}
        }

        # A session that is still alive but has gone quiet has also stopped
        # working, and the section is about work stopping — so it joins the dead
        # ones, carrying `alive` so the panel can render it undimmed and keep
        # counting it as fleet load. It is the *same row* it will still be when the
        # evictor gets to it a few minutes later, which is why the clock reads from
        # when it went quiet and not from when it was removed.
        #
        # This split is deliberately made here and not in `_reconciled_categories`:
        # that map feeds the HUD counts, the menu-bar strip and the simulator's face
        # slots, and a live session must not lose its frog for having been quiet for
        # two minutes. The consequence to know about is that the panel's `idle` KPI
        # now covers Sleeping *plus* the live rows here — one bucket shown in two
        # places, not two surfaces disagreeing.
        promoted = [
            e for e in out["sleeping"]
            # `idle_is_age` marks a reconciler-only row whose number is age since
            # start, not silence. A background agent still being listed is not one
            # that stopped, however old it is.
            if not e.get("idle_is_age")
            and FINISHED_IDLE_GRACE_SECONDS < e.get("idle_seconds", 0) <= FINISHED_RETENTION_SECONDS
        ]
        if promoted:
            keep = {id(e) for e in promoted}
            out["sleeping"] = [e for e in out["sleeping"] if id(e) not in keep]
            for entry in promoted:
                entry["alive"] = True
                entry["finished_at"] = time.time() - entry.get("idle_seconds", 0)
                out["finished"].append(entry)

        # Order within a section by what you'd act on first: the session that has
        # been blocked on you longest, the one that just did something, the one
        # that dozed off most recently. Insertion order (the default) carries no
        # meaning at all and reshuffles as sessions come and go.
        out["waiting"].sort(key=lambda e: e.get("idle_seconds", 0), reverse=True)
        out["running"].sort(key=lambda e: e.get("idle_seconds", 0))
        out["sleeping"].sort(key=lambda e: e.get("idle_seconds", 0))
        out["abandoned"].sort(key=lambda e: e.get("idle_seconds", 0), reverse=True)
        # Finished sorts the other way from the graveyard: here recency *is*
        # relevance — the run you just watched end is the one you came to look at.
        out["finished"].sort(key=lambda e: e.get("idle_seconds", 0))

        # Health signals last, because they read finished entries — and the whole
        # snapshot at once, since the questions worth asking are not all about one
        # session. The 5h budget is shared by the account, so "84% with three
        # agents running" belongs to the fleet rather than to whichever session
        # happened to report the number.
        #
        # Strictly additive: this attaches a key and computes nothing that
        # categorisation depends on. `_activity_counts` and `categorize()` stay
        # the single source of truth for what a session *is*; a signal only says
        # what someone might do about it.
        from . import signals as sig

        engine = self.__dict__.setdefault("_signals", sig.SignalEngine())
        # Finished sessions are not evaluated. A signal answers "what should you do
        # about this", and for a run that is over the answer is nothing — several
        # rules (ctx-full, churn, pr-changes) fire on state alone and would keep
        # advising you to /compact a session that has already stopped. Excluding the
        # bucket also keeps a dead session's last budget reading out of the
        # fleet-wide maximum, which is meant to describe what is running now.
        verdict = engine.evaluate({k: v for k, v in out.items() if k != "finished"})
        for entries in out.values():
            for entry in entries:
                entry["signals"] = verdict["by_session"].get(
                    entry.get("session_id", ""), [])
        # Rendered once, under the section bar. Repeating an account-level fact on
        # every row is how a panel teaches people to stop reading it.
        self._global_signals = verdict["global"]

        # Who is talking to whom. Fleet-level for the same reason the budget is:
        # an edge belongs to neither of its two rows.

        # Before the alert policy, because it can take work off it: a session
        # that is full and sitting in a VS Code window Dark Army can type into gets
        # `/compact` on its input line rather than a banner asking a human to
        # type the same thing. Order matters — the signals are marked `handled`
        # here, and that mark is what `alerts.py` reads a few lines down. See
        # autocompact.py for why the channel cannot carry this, and why one
        # attempt is the whole budget.
        self._decide_auto_compacts(out)

        # Which of those signals has earned an interrupt. Kept separate from the
        # engine above because the two answer different questions at different
        # cadences: a signal is evaluated every few seconds to be *shown*, an
        # alert fires once, when something changes, to be *delivered*.
        from . import alerts as alerting

        policy = self.__dict__.get("_alert_policy")
        if policy is None:
            policy = self._alert_policy = alerting.AlertPolicy(
                started_at=getattr(self, "_started_wall", 0.0))
        cards = dict(self._active_notifications)
        alerting.clear_resolved(policy, out, cards)
        # Open permission prompts ride into the same evaluation rather than
        # alerting from `_handle_channel_message` directly, and the detour is
        # the point: the relay handler knows a port and a request id, while the
        # policy's mute, the frontmost suppression and the nickname on the
        # banner all live here. The handler schedules a push the moment a
        # prompt lands, so "the next evaluation" is at most the push floor away
        # (AGENTS_PUSH_MIN_INTERVAL_SECONDS, ~a quarter second — accepted), and
        # delivery stays behind the snapshot — a surface told to reveal a
        # session it has not been shown has nothing to reveal.
        capture = getattr(self, "_decisions", None)
        if capture is not None:
            capture.reconcile(out, permissions=self._permission_snapshot)
        prompt_facts = self._prompts_by_session()
        # A copy of the policy's per-session stamps from *before* this
        # evaluation: `evaluate` overwrites them, and the buzz ledger wants
        # to say how long after the previous alert about this session the
        # new one came (`cooldown_gap`). Same thread, no other reader.
        before = dict(policy._last_per_session)
        report_hold = self._report_hold(policy, out)
        raised = policy.evaluate(out, cards, time.time(),
                                 suppressed=self._alert_suppressed(out),
                                 prompts=prompt_facts,
                                 panel_focused=self._panel_focused_sessions(),
                                 report_hold=report_hold)
        self._update_report_candidates(policy, out)
        if raised:
            # Bounded, and ordered oldest first: the tail is a record of what was
            # decided, kept so a surface that arrives late can see it.
            rows = [a.as_dict() for a in raised]
            if capture is not None:
                capture.freeze_alert_targets(rows, out,
                    (getattr(self, "_board_state", None) or {}).get("cards", []),
                    prompt_facts)
            # What the policy saw when it chose the kind, reduced to counts
            # and booleans on the same thread with the same inputs —
            # `_decision_ids`' precedent: an underscore key that rides the
            # two in-memory lists, is copied by nobody (the notifier and
            # `push_alert` take named keys) and is published nowhere. Read
            # back by `_ledger_buzz` at the drain.
            by_sid = {entry.get("session_id"): (name, entry)
                      for name, bucket in out.items() if isinstance(bucket, list)
                      for entry in bucket if isinstance(entry, dict)}
            for alert, row in zip(raised, rows):
                sid = alert.session_id
                gap = (alert.created_at - before[sid]) if sid in before else None
                name, entry = by_sid.get(sid, ("", None))
                row["_buzz_evidence"] = buzz_ledger.evidence(
                    entry, cards.get(sid), cooldown_gap=gap, category=name)
            self._alerts = (self._alerts + rows)[-32:]
            # And the queue for delivery, drained by whoever can actually post.
            # Separate from the tail above because the two mean different things:
            # `_alerts` is history, `_undelivered` is a debt. Appended from the
            # executor thread (this runs there); drained on the loop thread.
            self._undelivered.extend(rows)
            del self._undelivered[:-32]
        # After every walk that treats `out` as category → [row, …]. A
        # non-bucket key here would be an AttributeError in signals and
        # alerting, which is why this attaches last.
        out["project_facts"] = self._project_facts_snapshot(
            {(row.get("project") or "")
             for bucket in out.values() for row in bucket})
        # The host Mac's battery: a side effect on `self`, not a key on
        # `out` — the agents snapshot is the fleet, not the machine. Read
        # here so the one executor hop per push refreshes it.
        self._power_source_snapshot()
        return out

    def _decide_auto_compacts(self, snapshot: dict) -> None:
        """Which full sessions Dark Army will compact itself on this tick.

        Runs on the executor thread that builds the snapshot, so it decides and
        marks but never types: the send shells out to `ps` and fans HTTP at
        every VS Code window, and belongs on the loop (`_flush_auto_compacts`).
        Reachability is "this pid looks like a VS Code terminal and a 0.1.6+
        extension is loaded somewhere" — the send itself is what learns whether
        a window actually owns the tab. A miss calls `note_failed` and the
        session goes straight back to the alert path.

        Claude and Grok both qualify. `/compact` is a client command on both
        input lines; the thing that used to exclude Grok was the channel, which
        cannot expand a slash command for anyone.
        """
        from . import autocompact

        policy = self.__dict__.setdefault("_autocompact",
                                          autocompact.AutoCompactPolicy())
        policy.enabled = bool(self.auto_compact_enabled)
        now = time.time()
        live: set[str] = set()
        for category, entries in snapshot.items():
            if category == "finished":
                # A run that is over has nothing left to fill, so none of these
                # are *considered*. But the bucket also holds live sessions
                # quiet past FINISHED_IDLE_GRACE_SECONDS (120s), flagged
                # `alive` — and that is shorter than SETTLE_SECONDS (180s).
                # Leaving them out of `live` let `forget` below delete the
                # episode of a session in the middle of compacting (compacting
                # is quiet: that is what going quiet *looks* like here), so the
                # next tick opened a fresh episode and typed `/compact` twice.
                for entry in entries:
                    sid = entry.get("session_id") or ""
                    if (sid and entry.get("alive")
                            and entry.get("provider") != "codex"
                            and sid not in self._codex_records
                            and not sid.startswith("codex:")):
                        live.add(sid)
                continue
            for entry in entries:
                sid = entry.get("session_id") or ""
                if not sid:
                    continue
                if (entry.get("provider") == "codex"
                        or sid in self._codex_records or sid.startswith("codex:")
                        or self._session_provider(sid) == "codex"):
                    # Excluded from `live` too: forget obsolete held episodes,
                    # before consider() can suppress a fresh context warning.
                    continue
                live.add(sid)
                rows = entry.get("signals") or []
                metrics = entry.get("metrics") or {}
                ctx = metrics.get("ctx_used_pct")
                pid = entry.get("pid")
                provider = entry.get("provider") or "claude"
                # Only probe when the signal is up: `ps -E` per quiet session
                # every snapshot would be the habit this codebase keeps removing,
                # and consider() ignores `reachable` once an episode exists.
                reachable = (
                    autocompact.qualifies(rows)
                    and session_io.can_send_text(pid)
                )
                verdict = policy.consider(
                    sid, rows,
                    ctx if isinstance(ctx, (int, float)) else None,
                    reachable, now)
                if verdict == autocompact.PASS:
                    continue
                autocompact.mark(rows)
                if verdict == autocompact.SEND:
                    self._compact_queue.append((sid, pid, provider))
        policy.forget(live)

    def _consider_title(self, sid: str, transcript: str, stub: dict,
                        request: str = "") -> None:
        """Queue a session for naming, at most once, from the snapshot thread.

        Reading the opening prompt is the only work done here — the same read
        the fallback name is about to do, and already the cheapest part of this
        path. Everything with a cost in it (a subprocess, ten seconds, a token
        bill) happens in `_flush_session_titles` on the loop.

        `finished` and `abandoned` are skipped: their rows are tombstones with a
        half-life measured in minutes, and paying for a name they will wear
        until they are swept is the definition of spending on nothing. A
        session that is still alive gets named and *keeps* the name into its
        tombstone, which is the case worth having.

        `request` is for Grok, which has no Claude transcript: the caller
        already read `chat_history.jsonl`.
        """
        if stub.get("_category") in ("finished", "abandoned"):
            return
        self._titles_shop.enabled = bool(self.session_title_enabled)
        if not request:
            try:
                request = first_user_prompt(transcript)
            except Exception:                       # a transcript we cannot read
                return
        if self._titles_shop.consider(sid, request) == session_title.ASK:
            self._title_queue.append((sid, request))

    async def _flush_session_titles(self) -> None:
        """Name the sessions the snapshot just asked about, one at a time.

        Serial on purpose. This is a background nicety competing with the fleet
        it is describing for the same machine and the same account, and a burst
        of Claude Code processes is exactly what a monitor must not be: the
        queue drains at one title per snapshot cycle rather than eight at once
        when the daemon starts up in front of a busy day.

        Detached rather than awaited (`_title_task`), because the caller is the
        agents-push path: a ten-second helper on that path would hold the panel
        one full snapshot behind the fleet.
        """
        if not self._title_queue:
            return
        if self._title_task is not None and not self._title_task.done():
            return
        sid, request = self._title_queue.pop(0)
        self._title_task = asyncio.ensure_future(self._make_title(sid, request))

    async def _make_title(self, sid: str, request: str) -> None:
        """One `claude -p`, and whatever it says about this session.

        Every failure ends the same way — the shop remembers the miss, the row
        keeps the opening prompt it was already showing, and nothing is retried.
        A daemon that cannot find the CLI would otherwise fork one per session
        per snapshot, forever, to be told the same thing.
        """
        claude_bin = agents_poll.find_claude_binary()
        if not claude_bin:
            self._titles_shop.note_failed(sid)
            return
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *session_title.argv(claude_bin, request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # The state dir rather than the session's own directory: cwd is
                # what Claude Code resolves project config against, and a namer
                # has no business loading somebody's CLAUDE.md.
                cwd=str(STATE_DIR),
                env=subprocess_env.clean_env(),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=session_title.TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, OSError, ValueError):
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            self._titles_shop.note_failed(sid)
            logger.debug("session title for %s did not finish", sid[:12],
                         exc_info=True)
            return
        title = self._titles_shop.note(sid, out.decode("utf-8", "replace"))
        if title:
            logger.info("Named session %s: %s", sid[:12], title)
            # The name changed without the transcript changing, so nothing else
            # on this path would ever push it. Write it to history too: the
            # History tab reads `sessions.title`, and a generated name that
            # only lives in titles.json never reached that column.
            self._history_write("set_title", sid, title)
            self._schedule_agents_push()
        else:
            logger.debug("session title for %s was not a title", sid[:12])

    async def _flush_auto_compacts(self) -> None:
        """Type `/compact` into every session the policy just decided on.

        Drained rather than re-read, like the alert queue: a second push at a
        session that already got one is the thing the policy exists to prevent.
        A refusal is reported *back to the policy* rather than only logged —
        that is what re-arms the banner, and a compact that silently did not
        happen while Dark Army claimed it had is the one failure worth engineering
        against here.

        The bytes go through VS Code's `sendText`, not the channel. A slash
        command is expanded on the input line; the channel lands in the model's
        reading and the model has nothing that can compact.
        """
        if not self._compact_queue:
            return
        from . import autocompact

        pending, self._compact_queue = self._compact_queue, []
        policy = self.__dict__.get("_autocompact")
        seen: set[str] = set()
        for item in pending:
            # Older queue entries were a bare sid; keep that readable so a
            # mid-upgrade daemon cannot throw on the first flush.
            if isinstance(item, tuple):
                sid, pid, provider = (item + (None, None))[:3]
            else:
                sid, pid, provider = item, None, None
            if not sid or sid in seen:
                continue
            seen.add(sid)
            current_provider = self._session_provider(sid)
            if (provider == "codex" or current_provider == "codex"
                    or sid in self._codex_records or sid.startswith("codex:")):
                if policy is not None:
                    policy.note_failed(sid)
                self._schedule_agents_push()
                continue
            provider = current_provider or provider
            # Re-checked here, not where the episode was decided: the queue is
            # drained a tick later and a prompt can arrive in between. A relayed
            # tool-approval dialog owns the input line, and `sendText` ends in a
            # newline — which the dialog reads as "confirm the highlighted
            # choice". Dark Army would be approving a tool call nobody read, from a
            # feature the user enabled to save context. Hand it back to alerts
            # and try again on a later episode.
            if sid in self._prompts_by_session():
                logger.info("Auto-compact of %s deferred: a permission prompt "
                            "owns the input line", sid[:12])
                if policy is not None:
                    policy.note_failed(sid)
                continue
            if pid is None:
                st = self._session_states.get(sid) or {}
                pid = self._ensure_session_pid(sid, st) or self._roster_pid(sid)
            text = autocompact.command_for(provider or "claude")
            result = None
            if pid:
                # Clear the input line first. We cannot see what is on it, and
                # appending to a half-typed draft sends *and submits* something
                # the user never wrote (`please rewri/compact`). Ctrl-U is the
                # kill-line both harnesses honour, so the worst case becomes a
                # discarded draft rather than a submitted mangled one.
                result = await session_io.send_text(pid, "", INPUT_LINE_CLEAR + text)
            ok = bool(result and result.get("sent"))
            if ok:
                where = result.get("terminalName") or result.get("matchedBy") or "terminal"
                logger.info("Auto-compacting %s via %s", sid[:12], where)
            else:
                logger.info("Auto-compact of %s did not land; "
                            "handing it back to alerts", sid[:12])
                if policy is not None:
                    policy.note_failed(sid)

    def live_statusline_metrics(self) -> list[dict]:
        """Every session's most recent statusline payload, newest data included.

        The rate-limit windows are drawn from these, and the menu bar used to
        reach them through the agents snapshot it is *pushed*. That push fires on
        **structural** changes only, so after a restart with nothing starting or
        stopping it never arrived: the limits reader had no payloads, fell back
        to the on-disk cache, and rendered the dash for a window that had already
        reset — until some session happened to begin or end. This is the source
        those payloads come from, it is a dict in memory, and reading it costs
        nothing.
        """
        return [m for m in self._session_metrics.values() if isinstance(m, dict)]

    @staticmethod
    def _build_mesh(snapshot: dict) -> list[dict]:
        """Legacy shape from the same conservative identity resolver."""
        from . import collaboration
        return collaboration.legacy_mesh(collaboration.build(snapshot))

    def note_panel_visible(self, visible: bool) -> None:
        """The docked sidebar saying whether it is on screen.

        Called on the panel's stdout reader thread (`app._on_panel_action`
        special-cases it — no `callAfter`, no context push: a 5s heartbeat must
        cost two attribute writes, not a file-tree walk on the AppKit thread).
        Two scalar writes, read at the moment of use by `_alert_suppressed`,
        the same cross-thread pattern as `notification_sound_enabled`.

        **Nothing sends `True` any more.** The panel's own visibility
        heartbeat is gone; the surviving senders are `panel_process`'s EOF
        clear and an older panel binary, so this flag is a fail-open latch
        and never a fact `_panel_focused_sessions` may depend on — that set
        is carried by `panel_terminal`, which the pane already states only
        while it can be seen.
        """
        self._panel_visible = bool(visible)
        self._panel_visible_at = time.time()

    def note_panel_terminal(self, session_id: str) -> None:
        """The panel saying which session's Dark Army-owned terminal it is drawing
        (`""` for none). `note_panel_visible`'s path exactly: two scalar
        writes on the panel's stdout reader thread, read at the moment of use
        by `_alert_suppressed` under `FRONTMOST_TRUST_SECONDS`, so a panel
        that stops saying so re-arms the alerts within that window.

        **And the width hand-back.** While the pane is showing a session the
        Mac owns that pty's size (`terminal_phone_resize` is ignored); when
        the pane *comes back* to a session — newly in
        `_panel_focused_sessions()` after this write — the size the pane last
        stated (`_panel_pane_size`) is re-applied, so a phone that resized
        the pty while the panel was hidden hands the width back without the
        pane restating anything. Applied on the loop: the pty is the loop's.
        """
        before = self._panel_focused_sessions()
        self._panel_terminal_sid = str(session_id or "")
        self._panel_terminal_at = time.time()
        sid = self._panel_terminal_sid
        if sid and sid not in before and sid in self._panel_focused_sessions():
            self._reclaim_panel_pane_size(sid)

    def _reclaim_panel_pane_size(self, session_id: str) -> None:
        """Re-apply the pane's last stated size to `session_id`'s pty, on
        the loop whichever thread asks."""
        handle = self._pty_handle_for(session_id)
        size = self._panel_pane_size.get(handle or "") if handle else None
        if not size:
            return
        cols, rows = size
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            try:
                loop.call_soon_threadsafe(self._pty.resize, handle, cols, rows)
            except RuntimeError:
                pass
            return
        self._pty.resize(handle, cols, rows)

    def _panel_on_screen(self, now: Optional[float] = None) -> bool:
        """Whether the panel has said it is visible within
        `PANEL_VISIBLE_TRUST_SECONDS`. The panel is ordered out on hide, not
        torn down, so a pane it still holds keeps heartbeating — this is the
        daemon's own check that somebody can actually see it."""
        now = time.time() if now is None else now
        return bool(getattr(self, "_panel_visible", False)
                    and 0.0 <= now - getattr(self, "_panel_visible_at", 0.0)
                    <= PANEL_VISIBLE_TRUST_SECONDS)

    def _panel_focused_sessions(self) -> set:
        """The sessions the panel's terminal pane is showing, fresh — the set
        `AlertPolicy.evaluate` takes beside the VS Code frontmost set, and
        the width owner `terminal_phone_resize` defers to. Empty on every
        failure path, as the VS Code half is.

        **`panel_terminal` carries the visibility itself**, which is why
        there is no second conjunct here. `TerminalPane.swift` states the
        session id only `if client.visible`, sends `""` the moment
        `client.visible` goes false, and sends `""` on disappear; a panel
        that died stops heartbeating and `FRONTMOST_TRUST_SECONDS` (15s)
        empties the set. Anding in `_panel_on_screen()` looked like a
        belt-and-braces check and was in fact a permanent `set()`: nothing
        has sent `panel_visibility: True` since the panel's own visibility
        heartbeat was removed (`panel_process` sends only the EOF `False`),
        so both the width rule and this suppression were silently inert. One
        fact, one producer, and it is the pane's own heartbeat."""
        now = time.time()
        sid = getattr(self, "_panel_terminal_sid", "")
        if sid and 0.0 <= now - getattr(self, "_panel_terminal_at", 0.0) \
                <= FRONTMOST_TRUST_SECONDS:
            return {sid}
        return set()

    # ── Dark Army's own terminals ─────────────────────────────────────────────────

    def _pty_handle_for(self, session_id: str,
                        row: Optional[dict] = None) -> Optional[str]:
        """The Dark Army-owned terminal this session runs in, or None.

        The bind writes it (`_bind_dispatched_card`); before that, and for a
        session that came back after a restart of the hook stream, the
        session's pid is asked of `ptyhost.owns` — the same exact-pid or
        descent answer `session_io` uses. Two rules keep the answer from
        flapping between the dispatched session and the background agents
        it spawns, every one of which descends from the same child pid:

        - a terminal **already named** for another session is never
          re-pointed — it is not this session's, whatever its pid says;
        - an **unnamed** terminal is named here only when the caller hands
          the snapshot `row` and `_bindable_candidate` admits it (an
          interactive top-level row, never a background agent). Without a
          row (the frame and input reads) the handle is resolved but not
          remembered, and the next enrich names it.

        One rung sits between the name and the pid walk, for the row-less
        callers alone: **the Mission Control record is a second name for one
        terminal.** A quiet Mission Control is evicted on wall-clock
        staleness like any other row, and `_forget_session` unbinds its
        terminal's name — so for the seconds until the roster stub comes
        back and the enrich re-binds it, `for_session` misses and the state
        (and its pid) is gone. `_mission_handle_if_named` answers the
        recorded handle when the record's last-bound id is this one and the
        terminal at that handle is alive and not named for somebody else,
        **resolved only, never bound**: binding stays the row-driven leg
        behind `_bindable_candidate`, or a background agent Mission Control
        spawns could take the pane.
        """
        if not session_id:
            return None
        handle = self._pty.for_session(session_id)
        if handle is not None:
            self._mark_hosted(session_id)
            return handle
        if row is None:
            handle = self._mission_handle_if_named(session_id)
            if handle is not None:
                return handle
        st = self._session_states.get(session_id)
        pid = st.get("pid") if st else None
        if not pid:
            pid = self._roster_pid(session_id)
        if not pid:
            return None
        handle = self._pty.owns(pid)
        if handle is None:
            return None
        term = self._pty.get(handle)
        if term is None:
            return None
        if term.session_id and term.session_id != session_id:
            return None
        if row is not None:
            if not self._bindable_candidate(row):
                return None
            self._pty.bind(handle, session_id)
            self._mark_hosted(session_id)
        return handle

    def _mark_hosted(self, session_id: str) -> None:
        """Remember, on the session's own state, that it runs in a terminal
        Dark Army holds. Persisted with the rest of `sessions.json` (the pid is
        not), so after a restart the daemon still knows which rows had a
        terminal to come back to — the witness `_hostless_hosted_ids` reads.
        """
        st = self._session_states.get(session_id)
        if st is not None and not st.get("hosted"):
            st["hosted"] = True

    def _hostless_hosted_ids(self) -> list[str]:
        """Sessions Dark Army hosted whose terminal the broker no longer lists.

        A hosted session's process is the terminal's child: when the terminal
        is gone, so is the session, whatever `sessions.json` restored — a
        `waiting` row with a question nobody can answer, held for the whole
        staleness window because the pidless path spares waiters. The
        broker's own list is the stronger witness, and it is read only while
        the link is up (`connected`): an empty map during a reconnect is not
        a list of zero terminals. Off the persistent path the map is this
        process's own and is always current.
        """
        pty = getattr(self, "_pty", None)
        if pty is None or not pty.connected:
            return []
        return [sid for sid, st in self._session_states.items()
                if st.get("hosted") and pty.for_session(sid) is None]

    def _retire_hostless_sessions(self) -> list[str]:
        """Forget every session whose hosted terminal is gone, with the
        `terminal closed` verdict. Run once after the broker attach at
        startup — before the first snapshot, so a dead row is never drawn
        as one running in an editor — and by `_check_liveness` for a hook
        that never carried a pid."""
        gone = self._hostless_hosted_ids()
        for sid in gone:
            logger.info("Liveness: evicting session %s (its hosted terminal "
                        "is gone)", sid[:12])
            self._forget_session(sid, TERMINAL_CLOSED_REASON)
        if gone:
            self._persist_sessions()
        return gone

    def _pty_exited(self, handle: Optional[str]) -> bool:
        term = self._pty.get(handle) if handle else None
        return bool(term is None or term.exited)

    def conversation_source(self, session_id: str) -> tuple[str, str, str]:
        """``(provider, path, sid)`` for a published session's journal.

        Loop-side, cheap dict reads only. The three seams: Claude Code's
        stored journal (else a glob by id), a Codex rollout's ``path``,
        and Grok's ``chat_history.jsonl`` under its session directory.
        An unpublished session is ``("", "", "")`` and the pager is
        never asked.
        """
        row, _ = self._inbox_session_entry(session_id)
        if not isinstance(row, dict):
            return "", "", ""
        sid = str(row.get("session_id") or session_id or "")
        provider = str(row.get("provider") or "claude")
        path = ""
        if provider == "codex":
            record = self._codex_records.get(sid)
            if record is not None and getattr(record, "path", None):
                path = str(record.path)
        elif provider == "grok":
            folder = grok_roster.session_dir(sid, str(row.get("cwd") or ""))
            if folder is not None:
                path = str(folder / "chat_history.jsonl")
        else:
            state = self._session_states.get(sid) or {}
            path = str(state.get("transcript_path") or "") or ss.resolve_transcript(sid)
        return provider, path or "", sid

    async def image_preview(self, session_id: str, path: str) -> dict:
        """One picture from a published session's project, for the phone's
        Conversation tab (`image_preview.preview`). The session's working
        folder is read on the loop, from the same published row
        `conversation_source` reads; an unknown session is refused in words
        without touching disk. Everything else — confinement, decoding,
        shrinking — is one executor hop."""
        row, _ = self._inbox_session_entry(session_id)
        if not isinstance(row, dict):
            return {"available": False, "path": str(path or "")[:4096],
                    "reason": conversation.UNKNOWN_SESSION_REFUSAL}
        cwd = str(row.get("cwd") or "")
        # One picture at a time, whoever asks: a decode and a bounded folder
        # search each hold an executor thread, and a flood of reads must not
        # starve the daemon's other executor work.
        gate = getattr(self, "_image_gate", None)
        if gate is None:
            gate = self._image_gate = asyncio.Semaphore(1)
        loop = asyncio.get_running_loop()
        async with gate:
            return await loop.run_in_executor(
                None, image_preview.preview, cwd, path)

    async def conversation_page(self, session_id: str, since: int = 0,
                                key: str = "", agent: str = "") -> dict:
        """One page of a published session's conversation — or, with
        ``agent``, of one of its helpers' own journals.

        Resolve the source on the loop, then one executor hop into
        ``ConversationReader.page``. An unknown session is refused in
        words without touching disk.

        **A helper is read only under a session Dark Army publishes**, and
        only from that session's own ``subagents`` folder: ``agent`` must
        match ``conversation.AGENT_ID_RE`` (re-checked here, whatever the
        door checked) and names ``agent-<id>.jsonl`` there and nothing
        else. It need not still be live — a helper that just finished is
        still worth reading — but its file must exist. Claude Code only.
        """
        provider, path, sid = self.conversation_source(session_id)
        if not sid:
            return {"available": False,
                    "reason": conversation.UNKNOWN_SESSION_REFUSAL}
        sidechain = False
        if agent:
            if not re.fullmatch(conversation.AGENT_ID_RE, agent):
                return {"available": False,
                        "reason": conversation.NO_HELPER_REASON}
            if provider != "claude":
                return {"available": False, "provider": provider,
                        "reason": conversation.HELPER_PROVIDER_REASON}
            loop = asyncio.get_running_loop()

            def _helper_file(parent=path):
                # A filesystem stat: off the loop, like the page read itself.
                folder = ss._subagents_dir(parent)
                found = folder / f"agent-{agent}.jsonl" if folder else None
                return found if found is not None and found.is_file() else None

            helper = await loop.run_in_executor(None, _helper_file)
            if helper is None:
                return {"available": False, "provider": provider,
                        "reason": conversation.NO_HELPER_REASON}
            path, sidechain = str(helper), True
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._conversations.page, provider, path,
                              since, key, sidechain=sidechain))

    def terminal_frame(self, session_id: str, since: int = -1,
                       cols: Optional[int] = None, rows: Optional[int] = None,
                       *, resize: bool = False,
                       since_bytes: Optional[int] = None,
                       grid: bool = True, viewer: str = "",
                       phone_size: Optional[tuple] = None) -> dict:
        """One frame of a Dark Army-owned terminal's screen: the rows dirtied after
        `since`, bounded by `TERMINAL_MAX_BYTES` with `more` stated, and
        `overflowed` when the caller's revision can no longer be served
        incrementally (a resize or a screen swap since), in which case the
        frame is a full one. `available` is stated, never inferred.

        A frame that carries no rows is a **present** `unchanged: true`,
        never an omission — the phone's decoder is tolerant, and an omitted
        key would decode as a blank screen. Lines that have scrolled off
        the live screen ride as `history` (oldest first) on every changed
        frame, omitted on an unchanged poll so the panel keeps what it
        holds. With `grid=False` the row walk and `history` are skipped
        (`rows_changed` is `[]`, `unchanged` still stated) — the away
        phone feeds an emulator and reads only `data`.

        `since_bytes` is **the away phone's feed**: raw pty bytes for its
        own emulator, `-1` meaning "I hold nothing" — answered with
        `Screen.paint()` (`painted: true`), never the raw ring, which begins
        mid-sequence. A cursor that fell off the ring paints the same way
        with `ring_overflowed: true`. Otherwise the bytes since the cursor,
        at most `TERMINAL_POLL_RAW_BYTES`, with `data_more` when the rest
        is waiting and `bytes_read` the cursor to quote back.

        **The paint is bounded the same way.** A screen that drew a picture
        paints past one sealed reply, so the painted leg carries at most
        `TERMINAL_POLL_RAW_BYTES` with `data_more` stated and keeps the
        rest in `_terminal_paint_tail`, keyed on the cursor it just quoted.
        The phone quotes that cursor back on its next poll and gets the
        next slice — `painted: false`, `ring_overflowed: false`, the same
        cursor — until the tail is spent, after which the ring leg resumes
        from the anchor. The tail is served **above** the ring test, so a
        busy terminal cannot restart a drain already under way; a poll
        quoting any other cursor drops it. The remainder is kept **per
        viewer** — `viewer` is the sealed door's device id, `""` on
        loopback — because every paired phone polls the same handle, and
        a second phone's `-1` must not replace the first's tail or steal
        its next slice.

        `resize` is the panel's alone (the loopback read): there is one pty
        and one size. The phone's width rides its stream's `S` frame through
        `terminal_phone_resize`, honoured only while the panel's pane is not
        showing the session — and, **away**, rides the poll as
        `phone_size` through the same verb and the same owner rule: the
        away phone has no stream, so until 21 Sep 2026 the pty kept the
        Mac's width and every check-in replayed output laid out for a
        screen three times wider than the phone's, which the emulator
        wrapped into an unreadable smear. A size that actually changes the
        pty answers with a **paint** whatever cursor was quoted: the ring
        bytes before the change were drawn for the old width.
        """
        empty = {"available": False, "session": session_id, "revision": 0,
                 "cols": 0, "rows": 0, "rows_changed": [], "more": False,
                 "overflowed": False, "unchanged": False,
                 "reason": TERMINAL_NOT_OWNED_REFUSAL}
        handle = self._pty_handle_for(session_id)
        term = self._pty.get(handle) if handle else None
        if term is None:
            return empty
        if resize and cols and rows:
            self._pty.resize(handle, int(cols), int(rows))
        resized_for_phone = False
        if phone_size and not resize:
            want_cols, want_rows = phone_size
            if (want_cols, want_rows) != (term.cols, term.rows):
                resized_for_phone = self.terminal_phone_resize(
                    handle, session_id, want_cols, want_rows)
        screen = self._pty.screen(handle) or term.screen
        try:
            since_rev = int(since)
        except (TypeError, ValueError):
            since_rev = -1
        if grid:
            revision, changed, overflowed = screen.since(since_rev)
        else:
            revision, changed, overflowed = int(screen.revision), [], False
        out_rows: list = []
        size = 0
        more = False
        last_rev = since_rev
        for y, runs in changed:
            row_rev = screen.row_revision(y)
            blob = len(json.dumps([y, runs], separators=(",", ":")))
            # Never cut inside one revision's group of rows: the caller
            # quotes the last row's revision back and must get the rest.
            if out_rows and size + blob > TERMINAL_MAX_BYTES and row_rev != last_rev:
                more = True
                break
            out_rows.append([y, runs])
            size += blob
            last_rev = row_rev
        if more:
            revision = last_rev
        x, y = screen.cursor
        unchanged = bool(not out_rows and not overflowed
                         and revision == since_rev)
        frame = {
            "available": True,
            "session": session_id,
            "name": term.name,
            "revision": revision,
            "cols": term.cols,
            "rows": term.rows,
            "rows_changed": out_rows,
            "more": more,
            "overflowed": overflowed,
            "unchanged": unchanged,
            "cursor": [x, y],
            "cursor_visible": bool(screen.cursor_visible),
            "title": screen.title,
            "exited": term.exited,
            "returncode": term.returncode,
        }
        # Lines that have scrolled off the live screen. Omitted on an
        # unchanged poll so the panel keeps what it holds; present — even
        # as `[]` — whenever the live grid moved, so a cleared scrollback
        # is not left drawn.
        if not unchanged and grid:
            frame["history"] = screen.history_runs()
        if since_bytes is not None:
            try:
                cursor = int(since_bytes)
            except (TypeError, ValueError):
                cursor = -1
            if resized_for_phone:
                # The bytes before this poll were laid out for the old
                # width; the phone resets its emulator on a paint.
                cursor = -1
            painted = ring_overflowed = data_more = False
            data = b""
            total = int(term.bytes_read)
            self._prune_paint_tails()
            tail_key = (handle, str(viewer or ""))
            tail = self._terminal_paint_tail.get(tail_key)
            if tail is not None and tail[0] != cursor:
                # A poll quoting any other cursor is not the drain's; the
                # remainder is stale and is let go.
                self._terminal_paint_tail.pop(tail_key, None)
                tail = None
            if tail is not None:
                # The next slice of a paint already under way — above the
                # ring test, so a busy terminal cannot restart the drain.
                anchor, _stamp, rest = tail
                data = rest[:TERMINAL_POLL_RAW_BYTES]
                remaining = rest[len(data):]
                data_more = bool(remaining)
                total = anchor
                if remaining:
                    self._terminal_paint_tail[tail_key] = (
                        anchor, time.monotonic(), remaining)
                else:
                    self._terminal_paint_tail.pop(tail_key, None)
            elif cursor >= 0:
                chunk, total, ring_overflowed = self._pty.ring_since(handle, cursor)
                if cursor > total:
                    # A cursor from another life of this pty.
                    ring_overflowed = True
                if not ring_overflowed:
                    data = chunk[:TERMINAL_POLL_RAW_BYTES]
                    data_more = len(chunk) > len(data)
                    total = cursor + len(data)
            if tail is None and (cursor < 0 or ring_overflowed):
                # First contact, or the cursor fell off the ring: the
                # emulator's own drawing, never the ring's mid-sequence tail.
                try:
                    painted_bytes = screen.paint()
                except Exception:
                    logger.debug("paint failed for %s", handle, exc_info=True)
                    painted_bytes = b""
                # Bounded like the ring leg: one sealed reply's worth, the
                # rest kept for the next poll under the cursor quoted here.
                # `painted` is true on this first slice only — the phone
                # resets its emulator on every painted frame.
                data = painted_bytes[:TERMINAL_POLL_RAW_BYTES]
                data_more = len(painted_bytes) > len(data)
                painted = True
                total = int(term.bytes_read)
                if data_more:
                    self._terminal_paint_tail[tail_key] = (
                        total, time.monotonic(), painted_bytes[len(data):])
                else:
                    self._terminal_paint_tail.pop(tail_key, None)
            frame["data"] = base64.b64encode(data).decode("ascii")
            frame["bytes_read"] = total
            frame["painted"] = painted
            frame["ring_overflowed"] = bool(ring_overflowed)
            frame["data_more"] = data_more
        return frame

    def _prune_paint_tails(self) -> None:
        """Drop paint remainders for terminals the pty no longer knows and
        ones older than `TERMINAL_PAINT_TAIL_SECONDS`. Pruned by handle:
        a forgotten terminal drops every viewer's entry. Called on the
        loop, from `terminal_frame` alone."""
        if not self._terminal_paint_tail:
            return
        now = time.monotonic()
        for key in list(self._terminal_paint_tail):
            handle, _viewer = key
            _anchor, stamped, _rest = self._terminal_paint_tail[key]
            if (self._pty.get(handle) is None
                    or now - stamped > TERMINAL_PAINT_TAIL_SECONDS):
                self._terminal_paint_tail.pop(key, None)

    def _terminal_raw_input(self, handle: str, blob: bytes,
                            session_id: str = "") -> tuple[bool, str]:
        """Raw bytes from the desk into a Dark Army-owned terminal: the stream's
        `I` frame and the loopback `bytes` payload share this."""
        if len(blob) > TERMINAL_MAX_RAW_BYTES:
            return False, TERMINAL_TOO_LONG_REFUSAL
        if not blob:
            return True, ""
        if self._pty_exited(handle):
            return False, TERMINAL_EXITED_REFUSAL
        if not self._pty.write(handle, data=blob):
            return False, TERMINAL_EXITED_REFUSAL
        logger.debug("typed %d raw bytes into the terminal of %s",
                     len(blob), session_id[:12])
        return True, ""

    def terminal_attach(self, session_id: str) -> tuple:
        """The panel's stream asking for a pane: `(handle, paint, exited,
        refusal)`. `handle` is None with the refusal in words; `paint` is
        `Screen.paint()` of the terminal as it stands, the first thing the
        stream sends. An exited terminal still attaches — its last screen
        is the point of `EXITED_RETENTION_SECONDS` — and says so at once."""
        handle = self._pty_handle_for(session_id)
        term = self._pty.get(handle) if handle else None
        if term is None:
            return None, b"", False, TERMINAL_NOT_OWNED_REFUSAL
        screen = self._pty.screen(handle) or term.screen
        try:
            paint = screen.paint()
        except Exception:
            logger.debug("paint failed for %s", handle, exc_info=True)
            paint = b""
        return handle, paint, bool(term.exited), ""

    def terminal_stream_input(self, handle: str, blob: bytes) -> tuple[bool, str]:
        """An `I` frame. Desk rules: see `_terminal_raw_input`."""
        return self._terminal_raw_input(handle, blob)

    def terminal_stream_resize(self, handle: str, cols: int, rows: int) -> None:
        """The panel pane's `S` frame: the pane is the one owner of the pty's
        size while it is showing the session, and what it states is
        remembered (`_panel_pane_size`) so `note_panel_terminal` can hand
        the width back after a phone borrowed it."""
        try:
            cols, rows = int(cols), int(rows)
        except (TypeError, ValueError):
            return
        self._panel_pane_size[handle] = (cols, rows)
        self._pty.resize(handle, cols, rows)

    def terminal_phone_resize(self, handle: str, session_id: str,
                              cols: int, rows: int) -> bool:
        """The phone stream's `S` frame. **The width has one owner at a
        time**: while the panel's pane is showing `session_id`
        (`_panel_focused_sessions()`, the one source of truth) the Mac
        decides and this is ignored; once it is not, the phone's screen
        decides. True when applied. The refusal is logged once per stream,
        not once per heartbeat."""
        try:
            cols, rows = int(cols), int(rows)
        except (TypeError, ValueError):
            return False
        if session_id in self._panel_focused_sessions():
            if handle not in self._phone_resize_ignored:
                self._phone_resize_ignored.add(handle)
                logger.debug("phone resize of %s ignored: the panel's pane "
                             "is showing %s", handle, session_id[:12])
            return False
        self._phone_resize_ignored.discard(handle)
        self._pty.resize(handle, cols, rows)
        return True

    async def terminal_input(self, session_id: str, text: str, *,
                             from_phone: bool = False,
                             raw: bool = False,
                             data: Optional[bytes] = None) -> tuple[bool, str]:
        """Type one line, then Enter, into this session's Dark Army-owned terminal.

        Reaches a **narrower** set of sessions than `reply`,
        `answer_question` or `board_message` — only ones Dark Army owns the pty
        of: every pty Dark Army hosts, which is dispatched cards, ad-hoc
        terminals and Mission Control. Two routes:

        **Raw** (`raw` / `data`) takes the desk's rules from either door —
        no permission-prompt refusal, no drain wait, no control-character
        refusal — because the emulator is on the screen that typed it: the
        panel's pane at the desk, and the phone's own emulator (`Actions.swift`'s
        `terminal_input` with `bytes`), which shows the dialog `1` or `y`
        answers and sends Ctrl-C and Escape as a person would at the desk.
        Away, the gates are pairing, the lease, Face ID, the receipt token
        and `RELAY_MAX_KEY_WRITES_PER_MINUTE` (raw keys' own bucket).

        **Line** (`text`) keeps every refusal for an older phone app: a
        **leading slash** is typed from both doors, the same way the desk
        already types one; typing while an **open permission prompt** is up
        is `TERMINAL_PROMPT_REFUSAL` on both doors (the same set `can_close`
        reads — the dialog reads Enter as "confirm the highlighted
        choice"); from the phone, an empty line is `TERMINAL_EMPTY_REFUSAL`
        and a control character is `TERMINAL_CONTROL_REFUSAL`. The desk
        still types a bare Enter.
        """
        handle = self._pty_handle_for(session_id)
        if handle is None:
            return False, TERMINAL_NOT_OWNED_REFUSAL
        if self._pty_exited(handle):
            return False, TERMINAL_EXITED_REFUSAL
        if data is not None or raw:
            # The desk's own keys. No permission-prompt refusal: the dialog
            # is on this screen and `1` or `y` is how a person answers it.
            # No drain wait either — what the pty does not take at once is
            # queued in order (`PtyHost.write`), and a key that waited three
            # seconds for the CLI to read the previous one was the stall.
            blob = data if data is not None else (text or "").encode("utf-8", "replace")
            return self._terminal_raw_input(handle, blob, session_id)
        if session_id in self._prompts_by_session():
            return False, TERMINAL_PROMPT_REFUSAL
        text = str(text or "")
        if text.endswith("\n"):
            text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
        if "\n" in text or "\r" in text:
            return False, TERMINAL_ONE_LINE_REFUSAL
        if len(text) > TERMINAL_MAX_INPUT_CHARS:
            return False, TERMINAL_TOO_LONG_REFUSAL
        if from_phone:
            if not text.strip():
                return False, TERMINAL_EMPTY_REFUSAL
            # Every C0 byte and DEL, not just the two newlines above: `\x03`
            # twice quits the CLI, `\x1b` cancels a turn, `\x1b[A` recalls
            # history. `message_card`'s test, for its reason.
            if any(ord(c) < 32 or ord(c) == 127 for c in text):
                return False, TERMINAL_CONTROL_REFUSAL
        if not self._pty.write(handle, text + "\r"):
            return False, TERMINAL_EXITED_REFUSAL
        # A long line lands in halves on Darwin's ~1 KB pty queue; the
        # remainder is drained on the loop and waited for here, with a
        # bound. A wait that runs out is its own sentence — the process is
        # alive and simply not reading — never "has ended".
        if not await self._pty.drain(handle, TERMINAL_DRAIN_SECONDS):
            return False, TERMINAL_BUSY_REFUSAL
        logger.info("typed %d chars into the terminal of %s%s",
                    len(text), session_id[:12], " (phone)" if from_phone else "")
        return True, ""

    def _alert_suppressed(self, snapshot: Optional[dict] = None) -> set:
        """Sessions that must not interrupt right now regardless of their state.

        The rule: don't alert about the session whose terminal you are already
        looking at, because telling someone about the window in front of them is
        the fastest way to teach them to turn notifications off.

        This reads a cache rather than asking, and it does so from the executor
        thread — the answer comes from a fan-out to every VS Code window and
        cannot be awaited here. `_frontmost_checker` keeps the cache fresh, and
        an answer older than `FRONTMOST_TRUST_SECONDS` is discarded: a stale
        reading suppresses a window the user may have left minutes ago, and a
        withheld interruption is the expensive direction to be wrong in.

        The docked sidebar is the second reason not to interrupt: while it is
        on screen it is already showing the same news, so every session in the
        passed `snapshot` is suppressed — the snapshot rather than
        `_session_states`, so background-agent rows are covered too. The chime
        still plays; it never goes through `evaluate`. The reading **fails
        open**: it is trusted only while fresh (`PANEL_VISIBLE_TRUST_SECONDS`),
        and a future timestamp — clock weirdness — is distrusted the same way,
        so a dead or hung panel costs at most 15 seconds of silence. That
        second half is **currently inert in production**: nothing sends
        `panel_visibility: True` any more (`note_panel_visible`), so
        `_panel_on_screen()` is always false and the blanket suppression
        never fires. Kept, not deleted, because an older panel binary still
        sends the verb; the pane-focused half above stands on its own.
        """
        out: set = set()
        pids = self._frontmost_pids
        if pids and time.time() - self._frontmost_at <= FRONTMOST_TRUST_SECONDS:
            # `list(...)`: signal evaluation runs on the executor while the
            # loop mutates `_session_states`.
            for sid, st in list(self._session_states.items()):
                pid = st.get("pid")
                if pid and pid in pids:
                    out.add(sid)
        # The rebuilt half for a terminal VS Code cannot see: the session
        # whose Dark Army-owned terminal the panel says it is drawing, trusted for
        # the frontmost poll's own window and failing open exactly as it
        # does — a stale or future stamp contributes nothing.
        # **The width owner and this are two questions, and only the width
        # owner was asked.** `_panel_focused_sessions()` lost its
        # `_panel_on_screen()` conjunct so the pty's width has a live owner;
        # the conjunct stays *here*, because suppressing an alert is a much
        # stronger claim than owning a width. `client.visible` is true of a
        # panel open on a second monitor the person has walked away from,
        # and this set feeds one `pending` list that is drained to **both**
        # the Mac's banner and the phone's push (`_deliver_alerts`) — so a
        # panel left showing an agent's terminal would mute that agent's
        # buzz on a phone in the person's pocket, indefinitely. Nothing in
        # the terminal-stream plan asked for that. `_panel_on_screen()` is
        # false in production (no producer sends `panel_visibility: True`),
        # so alert behaviour here is exactly what it was before the width
        # rule was fixed, and the pane's suppression waits for a frontmost-
        # grade signal of its own.
        if self._panel_on_screen():
            out |= self._panel_focused_sessions()
        if snapshot and self._panel_on_screen():
            for entries in snapshot.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    sid = entry.get("session_id") or ""
                    if sid:
                        out.add(sid)
        return out

    def _update_report_candidates(self, policy, snapshot: dict) -> None:
        """The sleeping reporters still undecided after this evaluation: the
        frontmost poll asks about them next (`_suppression_candidates`).
        Executor thread; the set is replaced wholesale."""
        from . import alerts as alerting
        self._report_candidates = {
            entry.get("session_id") for entry in snapshot.get("sleeping") or []
            if isinstance(entry, dict) and entry.get("session_id")
            and alerting.report_pending(policy, entry)}

    def _report_hold(self, policy, snapshot: dict) -> set:
        """Sleeping reporters whose `report` banner must wait this tick: no
        frontmost reading that asked about the session has been taken since
        it went quiet, so "you are looking at it" cannot be ruled out.

        Only a session the poll can ask about (a recorded pid) is held, and
        only for `REPORT_FRONTMOST_WAIT_SECONDS` after it went quiet: past
        that the hold fails open, as every frontmost rule here does — a
        withheld banner is the expensive way to be wrong. Executor thread;
        reads the loop's wholesale-replaced scalars.
        """
        from . import alerts as alerting
        now = time.time()
        asked_at = float(getattr(self, "_frontmost_at", 0.0) or 0.0)
        asked = getattr(self, "_frontmost_asked", None) or set()
        held: set = set()
        for entry in snapshot.get("sleeping") or []:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("session_id") or ""
            if not sid or not alerting.report_pending(policy, entry):
                continue
            st = self._session_states.get(sid) or {}
            pid = st.get("pid")
            if not (isinstance(pid, int) and pid > 1):
                continue
            try:
                quiet = float(entry.get("quiet_since") or 0.0) \
                    or now - float(entry.get("idle_seconds") or 0.0)
            except (TypeError, ValueError):
                continue
            if now - quiet > REPORT_FRONTMOST_WAIT_SECONDS:
                continue
            if sid in asked and asked_at >= quiet:
                continue
            held.add(sid)
        return held

    def _suppression_candidates(self) -> dict:
        """session_id -> pid for the sessions an alert could currently be about.

        The whole poll hangs off this: `AlertPolicy` only ever fires on `waiting`
        or `crit`, so asking VS Code about a fleet of quietly-working sessions
        spends a fan-out and a `ps` walk per session to answer a question nobody
        asked. On an ordinary day this is empty and the checker does nothing.
        """
        categories = self._reconciled_categories()
        wanted = set(self._active_notifications.keys())
        wanted |= {sid for sid, cat in categories.items() if cat == "waiting"}
        # Sessions holding an open permission prompt, which may still be
        # categorised `running` — the hook event that flips them to `waiting`
        # races the relay. Honest limit: the prompt's *first* alert usually
        # beats the next 4s frontmost poll, so suppression catches it only when
        # the session was already a candidate. Delaying every permission alert
        # by a poll cycle to close that gap was considered and rejected — a
        # withheld interruption is the expensive direction to be wrong, and
        # this at least keeps repeat prompts from the same dialog suppressed.
        wanted |= set(self._prompts_by_session())
        # A sleeping row whose fresh work report has not been bannered yet:
        # the `report` rule must not tell you about the terminal in front of
        # you, and without a reading about this pid it could not know.
        wanted |= set(getattr(self, "_report_candidates", ()) or ())
        out = {}
        for sid in wanted:
            st = self._session_states.get(sid)
            pid = st.get("pid") if st else None
            if isinstance(pid, int) and pid > 1:
                out[sid] = pid
        return out

    async def _frontmost_checker(self) -> None:
        """Keep `_frontmost_pids` current, and only while it could matter.

        Deliberately its own timer rather than a step inside the agents push: the
        push runs on structural change and can fire several times a second, and
        this is an HTTP fan-out to every open VS Code window.
        """
        while self._running:
            await asyncio.sleep(FRONTMOST_POLL_SECONDS)
            try:
                candidates = self._suppression_candidates()
                if not candidates:
                    # Nothing to suppress: drop the reading rather than let it
                    # age into the trust window for whoever comes next.
                    self._frontmost_pids = set()
                    continue
                found = await vscode_reveal.frontmost_session_pids(
                    list(candidates.values()))
                self._frontmost_pids = found
                self._frontmost_asked = set(candidates)
                self._frontmost_at = time.time()
                # A held work report can now be decided: evaluate at once
                # rather than on the next slow refresh.
                if set(getattr(self, "_report_candidates", ()) or ()) \
                        & self._frontmost_asked:
                    self._schedule_agents_push()
            except Exception:
                # Never fatal, and never sticky: an unreachable window must not
                # leave a session suppressed.
                logger.debug("frontmost check failed", exc_info=True)
                self._frontmost_pids = set()

    @staticmethod
    def _subagent_rows(transcript: str, stats, agent_ids: list,
                       card_title: str = "") -> list[dict]:
        """Label a session's live subagents, each one under the agent that spawned it.

        Two sources, because Claude Code records a spawn wherever it happened.
        An agent launched by the session lands in the session transcript we just
        parsed (free — no extra I/O). An agent launched *by another subagent*
        leaves its record in that subagent's transcript, which the session never
        reads: to it, the agent is nameless. That is the whole reason nested
        agents used to render as a bare hex id. `load_subagent_meta` covers them
        from the meta file written beside every subagent transcript, at any
        depth, and names the parent so the rows can nest.

        Rows carry a `depth` (0 = spawned by the session) rather than a parent
        id, so a client can indent without walking the tree itself. **No client
        draws a nested row list today**: the panel and the phone each fold
        these into two composed strings — the CMD column's `subagentSummary`
        and the detail pane's helpers line.

        `card_title` is the parent session's own origin card, when Dark Army started
        it for one. It rides as a **second** key, never folded into the row's
        name: `SubagentRow.label` is matched as a `Set` against the card's
        declared workflow stages to decide which stage is running
        (`BoardCardView.liveStageNames`), so a qualified `label` would
        silently stop every card highlighting its running stage. The clients
        compose `qualified` from the pair.
        """
        from . import session_stats as ss

        title = " ".join(str(card_title or "").split())

        infos = {
            aid: ((stats.agents or {}).get(aid) or ss.load_subagent_meta(transcript, aid))
            for aid in agent_ids
        }

        def row(aid: str, depth: int) -> dict:
            info = infos.get(aid)
            out = {
                "agent_id": aid,
                "subagent_type": getattr(info, "subagent_type", "") if info else "",
                "description": getattr(info, "description", "") if info else "",
                "model": getattr(info, "model", "") if info else "",
                "activity": getattr(info, "activity", "") if info else "",
                "depth": depth,
                "parent_agent_id": getattr(info, "parent_agent_id", "") if info else None,
            }
            if title:
                out["card_title"] = title
            return out

        # A parent that isn't itself live (already finished, or beyond this
        # session) can't be nested under, so those agents sort as roots.
        children: dict[str, list[str]] = {}
        for aid in agent_ids:
            parent = getattr(infos.get(aid), "parent_agent_id", "") or ""
            children.setdefault(parent if parent in infos else "", []).append(aid)

        rows: list[dict] = []
        seen: set = set()

        def walk(parent: str, depth: int) -> None:
            for aid in children.get(parent, []):
                if aid in seen:
                    continue
                seen.add(aid)
                rows.append(row(aid, depth))
                walk(aid, depth + 1)

        walk("", 0)
        # A cycle in the parent links would strand rows unreachable from the
        # root. Never observed, but losing a live agent from the menu is worse
        # than showing it flat.
        rows.extend(row(aid, 0) for aid in agent_ids if aid not in seen)
        return rows

    def detailed_snapshot(self) -> dict:
        """Rich per-agent view for the menu dropdown, grouped into running /
        sleeping / waiting. Convenience wrapper (used by tests); production pushes
        split the cheap collect from the blocking enrich (see _push_agents_snapshot)."""
        from . import collaboration
        snapshot = self._enrich_agent_stubs(self._collect_agent_stubs())
        self._apply_grok_live_subagents(snapshot)
        self._apply_grok_finished_demote(snapshot)
        self._collaboration = collaboration.build(
            snapshot, (getattr(self, "_board_state", None) or {}).get("cards", []))
        self._mesh = collaboration.legacy_mesh(self._collaboration)
        return snapshot

    def _assign_nicknames(self, parsed: list) -> dict[str, str]:
        """Nickname per session id, collisions resolved against live rows only.

        Two passes, and the order is the whole point. Sessions that already hold
        a name keep it and define what is taken; only then do new sessions pick,
        against a set that is already complete. One pass would let an arriving
        session grab a name a running one is about to be confirmed in, and rename
        a row you were looking at.

        Finished tombstones are deliberately included: the row is still on
        screen, so its name is still spoken for.

        Two sessions can walk in holding the same name, now that a name is not
        released by going quiet: an agent nobody has seen for hours kept
        `Cipher`, a session that started meanwhile was offered it (`taken` is
        built from what is *on screen*, which is the rule that makes the cast
        usable at all), and then the first one's human typed. One of them has
        to yield, and it is the newcomer to the snapshot — the row already
        wearing the name is the one somebody may be reading. Deterministic when
        neither was showing it, because Python's dict order is not a decision.

        A session Dark Army dispatched for a known job takes its name from that job's
        pool rather than from the hash (`_role_nickname`), so the face on
        screen means the work. It is only ever a *first* pick: a session that
        already holds a name keeps it, above, and one Dark Army did not dispatch has
        no role and gets exactly today's answer.
        """
        assigned: dict[str, str] = {}
        held = self._identities.assigned()
        previous = self._nicknames_shown
        roles = self._card_roles_by_session()

        claims: dict[str, list[str]] = {}
        pending: list[str] = []
        for stub, _transcript, _stats in parsed:
            sid = stub["session_id"]
            if sid in held:
                claims.setdefault(held[sid], []).append(sid)
            else:
                pending.append(sid)

        taken = set(claims)
        for name, claimants in claims.items():
            if len(claimants) == 1:
                keeper = claimants[0]
            else:
                showing = [s for s in claimants if previous.get(s) == name]
                keeper = (showing or sorted(claimants))[0]
            assigned[keeper] = name
            self._identities.touch(keeper)
            for loser in claimants:
                if loser != keeper:
                    self._identities.forget(loser)
                    pending.append(loser)

        stubs = {stub["session_id"]: stub for stub, _t, _s in parsed}
        for sid in pending:
            kind, card_id, area = (roles.get(sid)
                                   or self._card_role_from_origin(sid, stubs.get(sid) or {}))
            name = self._identities.name_for(
                sid, taken,
                preferred=self._role_nickname(kind, card_id, area, taken))
            assigned[sid] = name
            taken.add(name)

        # Names are *not* released when a session leaves the snapshot, and that
        # is the fix for a real rename. Absence looked like the honest test and
        # is not: a session evicted for going quiet comes back the moment its
        # human types again — one did, after seven hours — and by then its
        # assignment was gone, so it re-picked. The pick is deterministic but
        # the *collision set* is not, so the same session came back as a
        # different character while its terminal tab still wore the old badge.
        # An agent that is renamed while you are away is exactly what
        # `IdentityStore`'s "sticky for the session's lifetime" rule promises
        # cannot happen.
        #
        # Absence was also doing the bounding, which was the other half of its
        # case. It does not need to: MAX_SESSION_ENTRIES bounds the map on
        # save, and `touch` above makes that prune drop the oldest *agent*
        # rather than the oldest entry. Collisions are unaffected — `taken` is
        # built from this snapshot's rows, so a name held only by a remembered
        # absent session was never withheld from anyone.
        self._identities.save()
        self._nicknames_shown = assigned
        return assigned

    def _session_name(self, sid: str, stub: dict, transcript,
                      reg=None) -> str:
        """Best available human name for a session, in order of authority.

        0. A name the user actually typed. `~/.claude/sessions/<pid>.json` marks
           it `nameSource: "explicit"` after `/rename`, which is the only way to
           distinguish a chosen name from the default slug rejected at step 3.
           It outranks the statusline because it is the same instruction read
           first-hand, and it is there even for a session that has not yet ticked.
        1. The statusline's `session_name`. Claude Code populates it from `--name`
           / `/rename`, falling back to the AI title, and — crucially — leaves it
           *absent* when all it would have is the default display name. So when it
           is there, it is the real thing.
        2. The AI title derived from the transcript, then the one Dark Army generated
           itself when Claude Code wrote none (`session_title.py`), then — as a
           name of last resort, and the reason that module exists — the opening
           request, which is a description of the work only by accident.
        3. The CLI's name from `claude agents --json` — but only when it is a real
           one. For background agents it is descriptive ("Analyze and reduce
           excessive automation tests"); for interactive ones it is the default
           display slug, project plus two hex ("shop-front-00"), which says nothing
           the row does not already say one line below in the project column.
        4. UNNAMED_SESSION. A session that genuinely has no name should say so,
           rather than wear a session id or a slug as a disguise.
        """
        if reg is not None and reg.named_by_user:
            return reg.name.strip()

        if stub.get("provider") == "grok":
            grok_name = (stub.get("cli_name") or "").strip()
            if grok_name:
                return grok_name
            generated = self._titles_shop.title_for(sid)
            if generated:
                return generated
            request = grok_roster.first_prompt(sid, stub.get("cwd") or "")
            if request:
                self._consider_title(sid, "", stub, request=request)
                generated = self._titles_shop.title_for(sid)
                if generated:
                    return generated
                return " ".join(request.split())
            return UNNAMED_SESSION

        metrics = self._session_metrics.get(sid) or {}
        statusline_name = (metrics.get("session_name") or "").strip()
        if statusline_name:
            return statusline_name

        if transcript:
            generated = self._titles_shop.title_for(sid)
            if not generated:
                # Nothing above answered, so the row is about to wear the
                # request as its name. That is the trigger, and it is read from
                # the same transcript the fallback comes from: ask for a title,
                # once, and keep showing the request until one arrives.
                self._consider_title(sid, transcript, stub)
            derived = session_display_name(transcript, sid, generated)
            # session_display_name never fails — its last resort is the short id,
            # which is one of the disguises we are trying to get rid of.
            if derived and derived != sid[:8]:
                return derived

        cli_name = (stub.get("cli_name") or "").strip()
        if cli_name and not _is_default_slug(cli_name, stub.get("project", "")):
            return cli_name
        return UNNAMED_SESSION

    def _schedule_agents_push(self) -> None:
        """Coalesced fire-and-forget of the detailed agent snapshot to the menu.
        _enrich_agent_stubs does file I/O, so it runs in an executor; bursts of
        state changes collapse to at most one push in flight plus one trailing
        push."""
        if not self._observers_implementing("on_agents_change"):
            return
        task = self.__dict__.get("_agents_push_task")
        if task is not None and not task.done():
            # A push in flight does NOT cover this change: it snapshotted the
            # session state when it started. Dropping the request here is how a
            # SubagentStart landing mid-push stayed out of the menu until some
            # unrelated event happened to trigger another one — while the window's
            # HUD, fed by the un-throttled counts path, showed it immediately.
            self._agents_push_pending = True
            return
        self._agents_push_task = asyncio.ensure_future(self._push_agents_snapshot())

    #: Floor between two agent snapshots. Not a rate limit — the coalescing
    #: above already collapses a burst into one push plus a trailing one — but a
    #: floor under how *closely* those two can land. Without it a busy fleet
    #: pushed three full payloads inside 34ms, and every push rebuilds the
    #: panel's view graph: a click whose mouse-down and mouse-up straddle one is
    #: cancelled, which is what made rows in the attention section unopenable.
    #: 250ms is under the shortest interval a person can act inside and an order
    #: of magnitude above the storm.
    AGENTS_PUSH_MIN_INTERVAL_SECONDS = 0.25

    #: The heavy half of a push — the roster re-read, navigation proofs, the
    #: checklist, collaboration, terminal titles and the board reconcile —
    #: runs when `_structural_key` moved or at least this often; every other
    #: push is the light tier (enrich, observers, flushes, alerts). The floor
    #: is the correctness bound for everything the key does not see: an AI
    #: title landing, `BOARD_SESSION_GRACE`, the bind window, the queue drain
    #: and lifecycle samples (`metrics.MAX_GAP` is 30s). Not a preference;
    #: keep it well under 30.
    FULL_PUSH_INTERVAL_SECONDS = 5.0

    def _agents_push_delay(self, now: float) -> float:
        """How long this push should wait so it does not tread on the last one.
        Zero after any quiet moment, which is the ordinary case."""
        last = self.__dict__.get("_last_agents_push")
        if last is None:
            return 0.0
        return max(0.0, self.AGENTS_PUSH_MIN_INTERVAL_SECONDS - (now - last))

    #: The stub fields `_structural_key` reads — every field
    #: `_collect_agent_stubs` writes except the clocks (`idle_seconds`,
    #: `quiet_since`, `started_at`), the run-health counters and
    #: `current_tool`. A clock or a metric here would make every statusline
    #: push a full one; `current_tool` moves on every tool call of every
    #: working session — measured, it was nearly every key move — and
    #: nothing in the full tier reads it (the signals that do run in the
    #: enrich, on every push), so it rides the light tier alone.
    _STRUCTURAL_STUB_FIELDS = (
        "session_id", "state", "subagents", "subagent_ids", "pid", "project",
        "cwd", "provider", "kind", "agent_activity", "cli_name",
        "ask_note", "tab_gone", "_category",
    )

    def _structural_key(self, stubs: list[dict]) -> tuple:
        """What, if it moved, makes this push a full one. Loop only.

        The stubs' structural fields plus the notification cards, open
        permission prompts, questions, both hookless rosters (Codex by
        revision), the tombstones and the board store's change counter
        (which leaves out the observation passes' own writes). A
        field the surfaces draw that this omits still lands — on the light
        tier's observers at once, and in the full tier's reconcile within
        `FULL_PUSH_INTERVAL_SECONDS`."""
        def frozen(value):
            if isinstance(value, (set, frozenset)):
                return tuple(sorted((frozen(v) for v in value), key=repr))
            if isinstance(value, (list, tuple)):
                return tuple(frozen(v) for v in value)
            if isinstance(value, dict):
                return tuple(sorted((k, frozen(v)) for k, v in value.items()))
            return value
        rows = tuple(
            tuple(frozen(stub.get(field)) for field in self._STRUCTURAL_STUB_FIELDS)
            for stub in stubs)
        return (
            rows,
            tuple(sorted(self._active_notifications)),
            tuple(sorted(self._prompts_by_session())),
            tuple(sorted(self._questions)),
            tuple(sorted((sid, frozen(r.revision))
                         for sid, r in self._codex_records.items())),
            tuple(sorted(self._grok_records)),
            tuple(sorted(self._finished)),
            self._board_change_counter(),
        )

    def _board_change_counter(self) -> int:
        """The attached store's write counter, 0 with no board (or a stand-in
        store without one)."""
        counter = getattr(getattr(self, "_board", None), "change_counter", None)
        if not callable(counter):
            return 0
        try:
            return counter()
        except Exception:
            logger.debug("board change counter unavailable", exc_info=True)
            return 0

    def _apply_terminal_titles(self, snapshot: dict, codex_title_roots,
                               shared_targets=()) -> None:
        """Resolve private Codex destinations and write all titles on a worker."""
        codex_title_ttys = codex_rollouts.resolve_title_ttys(codex_title_roots)
        codex_title_ttys = codex_titles.title_ttys(shared_targets, codex_title_ttys)
        self._titles.apply(snapshot, codex_title_ttys)

    async def _push_agents_snapshot(self) -> None:
        self._prune_refinement_receipts()
        if not self._observers_implementing("on_agents_change"):
            return
        delay = self._agents_push_delay(time.monotonic())
        if delay:
            # Deliberately here rather than in `_schedule_agents_push`: the wait
            # happens with this push marked as in flight, so everything that
            # arrives during it collapses into the trailing push instead of
            # queueing up behind the sleep.
            await asyncio.sleep(delay)
        self._last_agents_push = time.monotonic()
        loop = asyncio.get_event_loop()
        # Two tiers. The full one re-reads the world — rosters, navigation
        # proofs, checklist, collaboration, titles, the board reconcile — and
        # runs when the structural key moved or `FULL_PUSH_INTERVAL_SECONDS`
        # lapsed; the light one keeps the numbers fresh on every push. The
        # floor half is decided before the stubs so the roster refresh can
        # land first and the snapshot is built on rosters no older than it.
        now = time.monotonic()
        last_full = getattr(self, "_last_full_push_at", None)
        full = last_full is None or now - last_full >= self.FULL_PUSH_INTERVAL_SECONDS
        try:
            if full and (self._roster_refreshed_at is None
                         or now - self._roster_refreshed_at >= ROSTER_REFRESH_INTERVAL):
                # Through the one in-flight task: a refresh the statusline or
                # the frontmost poll already started is awaited, not doubled.
                try:
                    await self._schedule_roster_refresh(announce=False)
                except Exception:
                    logger.warning("roster refresh failed", exc_info=True)
            stubs = self._collect_agent_stubs()          # loop thread: safe copy
            key = self._structural_key(stubs)
            full = full or key != getattr(self, "_last_structural_key", None)
            if full:
                self._last_full_push_at = now
                self._last_structural_key = key
                self._registry_memo = None
            self._prune_refinement_receipts()
            codex_title_roots = codex_rollouts.project_title_roots(
                self._codex_records.values()
            )
            if full:
                self._refresh_codex_input()
                await self._refresh_codex_terminals()
                captured = self._navigation_capture()
                try:
                    proofs = await asyncio.wait_for(loop.run_in_executor(
                        None, codex_rollouts.resolve_navigation_proofs, codex_title_roots), 5.0)
                except asyncio.TimeoutError:
                    proofs = {}
                if self._navigation_capture_current(captured):
                    self._codex_navigation = MappingProxyType(proofs)
                    for stub in stubs:
                        if stub.get("provider") == "codex":
                            stub.update(self._session_capabilities(stub))
            # The light tier's stubs already carry capabilities off the
            # cached `_codex_navigation` (`_collect_agent_stubs`).
            snapshot = await loop.run_in_executor(        # executor: transcript I/O
                None, self._enrich_agent_stubs, stubs
            )
            self._apply_grok_live_subagents(snapshot)
            self._apply_grok_finished_demote(snapshot)
            # The first-run checklist's facts, off the same cycle. Its own
            # try: a failure here is a stale checklist, never a lost fleet.
            if full:
                try:
                    self._checklist_facts = await loop.run_in_executor(
                        None, self._observe_checklist, snapshot)
                except Exception:
                    logger.debug("checklist observation failed", exc_info=True)
        except Exception:
            # Warning for `board reconcile failed`'s reason: this is the whole
            # push — the titles, the observers every surface reads, the
            # reconcile — and a failure here is a fleet that has quietly
            # stopped, not a detail.
            logger.warning("agents snapshot push failed", exc_info=True)
            # Whatever this push was meant to reconcile, the next one does.
            self._last_structural_key = None
            self._drain_pending_agents_push()
            return
        # Final provider-adjusted picture, using captured primitive board links.
        # Full tier only: the light tier keeps the last projection.
        if full:
            from . import collaboration
            cards = [{field: card.get(field) for field in
                      ("id", "root", "title", "session_id", "refine_session_id")}
                     for card in (getattr(self, "_board_state", None) or {}).get("cards", [])]
            try:
                evidence = await loop.run_in_executor(None, collaboration.build, snapshot, cards)
                mesh = collaboration.legacy_mesh(evidence)
            except Exception:
                logger.warning("collaboration projection failed", exc_info=True)
                evidence, mesh = collaboration.unavailable(), []
            self._collaboration, self._mesh = evidence, mesh
        # The terminal tabs, from the snapshot that was just built. Before the
        # observers rather than after, so the tab and the panel agree on the same
        # reading, and in the executor because it opens ttys and shells out to
        # `ps` — neither belongs on the event loop. Failure is logged and dropped:
        # this is decoration on a path that seven execution flows run through, and
        # a terminal that will not take a write must not cost anyone their panel.
        # Exited Dark Army-owned terminals are forgotten **here, on the loop** —
        # the reap takes the master fd off the selector before closing it,
        # and the selector is the loop thread's alone. On the titles worker
        # it closed the fd under a live key and a reused fd number was never
        # read again.
        try:
            self._pty.reap()
        except Exception:
            logger.debug("reaping exited terminals failed", exc_info=True)
        for handle in list(self._panel_pane_size):
            if self._pty.get(handle) is None:
                self._panel_pane_size.pop(handle, None)
                self._phone_resize_ignored.discard(handle)
        if full:
            try:
                # Copy immutable, still-visible targets on the loop. Never let
                # the worker read the mutable roster or grant a public PID.
                shared_targets = ()
                if time.monotonic() - getattr(self, "_codex_titles_observed_at", 0) < 30:
                    shared_targets = tuple(
                        target for sid in self._codex_records
                        if (target := self._codex_terminal_current(sid)) is not None
                    )
                await loop.run_in_executor(
                    None, self._apply_terminal_titles, snapshot, codex_title_roots,
                    shared_targets
                )
            except Exception:
                logger.debug("terminal title update failed", exc_info=True)
        # Outside the try: a raising observer is that observer's problem, and
        # _notify_observers already isolates it. Swallowing it here as a "push
        # failure" would hide a bug in one surface behind a debug-level log.
        # Assigned *before* observers: `on_agents_change` broadcasts `state()`,
        # and inbox prune must not see a missing cache as an empty fleet.
        # Rebound, never mutated: the snapshot itself belongs to whoever is
        # reading it. `_known_project_roots` and inbox live-keys both read it.
        self._agents_snapshot_cache = snapshot
        # Stamped again at delivery: the floor is between two frames a
        # surface *receives*, and a full push's awaits (the roster read, the
        # proofs, the enrich) can outlast it, so a trailing push measured
        # from this one's start would land right behind it.
        self._last_agents_push = time.monotonic()
        self._notify_observers("on_agents_change", snapshot)
        # The board's own reconcile, after the snapshot rather than before, and
        # for `_deliver_alerts`' reason: a card naming a session must never reach
        # a surface before the snapshot that lists it. On the executor because it
        # is SQLite; the announcement comes back to the loop.
        # Full tier only; `FULL_PUSH_INTERVAL_SECONDS` bounds how late a
        # time-based leg (the grace, the bind window, lifecycle samples) runs.
        if full:
            try:
                # The pass's own ledger writes do not move the key: the
                # store's `change_counter` leaves them out.
                changed = await loop.run_in_executor(
                    None, self._reconcile_board, snapshot)
                if changed:
                    await self._publish_board()
            except Exception:
                # **Warning, not debug.** Everything that keeps the board honest
                # runs in here — the bind, the give-up that frees a project's
                # launch slot, the filer, the importance number — so a raise on
                # this path freezes all of it until the daemon is restarted, and
                # at debug level it did that in silence for a day.
                logger.warning("board reconcile failed", exc_info=True)
        # The queue the reconcile just decided on. Immediately after it and
        # before everything else here, because it is the only one of these
        # flushes that opens a window on somebody's screen — a person watching
        # a session end should see the next card start, not see it start after
        # two subprocess helpers have had their turn.
        try:
            await self._flush_queue_dispatches()
        except Exception:
            logger.debug("queue drain failed", exc_info=True)
        # The compacts the snapshot decided on, before the alerts it decided on:
        # a push that fails re-arms the banner, and the session should hear
        # about that on the next tick rather than after a banner it was
        # supposed to replace has already gone out.
        try:
            await self._flush_auto_compacts()
        except Exception:
            logger.debug("auto-compact push failed", exc_info=True)
        # And the names the snapshot asked for. Last on this path, and detached
        # once started: a session's name is the least urgent thing here, and the
        # only one that costs a subprocess.
        try:
            await self._flush_session_titles()
        except Exception:
            logger.debug("session title request failed", exc_info=True)
        # And the scores the reconcile asked for — the titles' sibling, with
        # the same shape for the same reason: serial, detached, one card per
        # snapshot cycle, never a burst.
        try:
            await self._flush_card_priorities()
        except Exception:
            logger.debug("card priority request failed", exc_info=True)
        # And the records the reconcile asked for. The filer's sibling, same
        # shape for the same reason: serial, detached, one card per snapshot
        # cycle, never a burst of git processes.
        try:
            await self._flush_work_records()
        except Exception:
            logger.debug("work record collection failed", exc_info=True)
        # After the snapshot, not before: an alert names a session, and a surface
        # told to reveal one it has not been shown yet has nothing to reveal.
        self._deliver_alerts()
        # The phone's live card, right after the buzz: the standing reading
        # of who is at the top of Needs you, pushed only where it changed.
        # Its own try/except — the push leg never takes the snapshot down.
        try:
            self._push_live_activity(snapshot)
        except Exception:
            logger.warning("phone live card push failed", exc_info=True)
        self._drain_pending_agents_push()

    def _deliver_alerts(self) -> None:
        """Hand raised alerts to whoever can post them, exactly once.

        Drained rather than re-read, because delivery is not idempotent: a
        surface that reconnects must not replay this afternoon's interruptions.
        If nothing implements `on_alerts` the queue is still cleared — an alert
        nobody could deliver is stale within seconds, and keeping it would mean
        a burst the moment a deliverer appears.

        Drained **once**, above both deliveries: the Mac banner
        (`on_alerts` → the menu bar's notifier) and the phone push, which
        must still fire with zero `on_alerts` observers — a daemon whose
        menu-bar process is mid-restart is exactly when the phone is the
        only surface left.

        The banner leg receives the whole batch, at once. The phone leg is
        handed the same batch and filters, gates and holds it on its own
        (`_start_phone_leg`): the one place a drained alert is looked at
        again, stated in `docs/session-state-contract.md`, *Alerts*.
        """
        if not self._undelivered:
            return
        pending, self._undelivered = self._undelivered, []
        self._deliver_batch(pending)

    def _deliver_batch(self, pending: list) -> None:
        """Both deliveries for one batch already out of the queue: the Mac
        banner (`on_alerts`, the whole batch at once) and the phone leg.
        `_deliver_alerts` hands it the drain; `_raise_access_alert` its own
        row alone, leaving `_undelivered` untouched."""
        observers = self._observers_implementing("on_alerts")
        if observers:
            self._notify_observers("on_alerts", pending)
        else:
            logger.debug("Dropping %d alert(s): nothing can post a banner",
                         len(pending))
        try:
            self._start_phone_leg(pending, banner=bool(observers))
        except Exception:
            # The push leg must never take the banner leg down with it.
            logger.warning("phone leg failed to start", exc_info=True)

    def _start_phone_leg(self, pending: list, *, banner: bool) -> None:
        """Hand a drained batch to the phone leg.

        On the loop, one task (`_phone_leg`) kept in `_phone_leg_tasks` until
        it finishes; off the loop — plain sync code in a test — at once
        through `_phone_leg_now`, `_log_event`'s split. A drain after
        `_shutdown` began starts nothing: the task would outlive the
        cancellation sweep and write to a closed ledger.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._phone_leg_now(pending, banner=banner)
            return
        stopping = getattr(self, "_shutdown_event", None)
        if stopping is not None and stopping.is_set():
            logger.debug("phone leg skipped for %d alert(s): shutting down",
                         len(pending))
            return
        tasks = self.__dict__.setdefault("_phone_leg_tasks", set())
        task = loop.create_task(self._phone_leg(list(pending), banner=banner))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @staticmethod
    def _split_finished(pending: list) -> tuple[list, list]:
        """`(finished, rest)` — S3: a `finished`-kind alert never reaches
        `_push_phone_alerts`. Identity-preserving; order kept."""
        finished, rest = [], []
        for alert in pending:
            if isinstance(alert, dict) and alert.get("kind") == "finished":
                finished.append(alert)
            else:
                rest.append(alert)
        return finished, rest

    @staticmethod
    def _split_unlisted(pending: list, listed) -> tuple[list, list]:
        """`(unlisted, rest)` — an alert whose session the phone's Needs you
        list does not show (`live_activity.listed_sessions`) never reaches
        `_push_phone_alerts`: `clearDeliveredIfQuiet` on the phone would
        sweep it away at its next check-in. The `security` kind is never
        unlisted (the person's decision, 23 Sep 2026): that sweep runs only
        on a foreground snapshot, so the buzz stays on the Lock Screen until
        the app is opened, and with Mac banners off it is the only interrupt
        for a door burst. Any other alert with an empty session id is
        unlisted. ``listed`` of ``None`` (the list could not be read) gates
        nothing. Identity-preserving; order kept."""
        if listed is None:
            return [], list(pending)
        unlisted, rest = [], []
        for alert in pending:
            sid = str(alert.get("session_id") or "") \
                if isinstance(alert, dict) else ""
            if isinstance(alert, dict) \
                    and alert.get("kind") == alerting.KIND_SECURITY:
                rest.append(alert)
            elif sid and sid in listed:
                rest.append(alert)
            else:
                unlisted.append(alert)
        return unlisted, rest

    def _withhold_unlisted(self, pending: list, *, banner: bool) -> list:
        """The unlisted rung of both phone legs: read the phone's list once,
        write each unlisted alert down `withheld:unlisted`, return the rest."""
        unlisted, rest = self._split_unlisted(
            pending, self._phone_listed_sessions())
        for alert in unlisted:
            self._ledger_quietly([alert], "withheld:unlisted", banner=banner)
        return rest

    def _ledger_quietly(self, pending: list, outcome, *, banner: bool,
                        idle_seconds=None, held_seconds: float = 0.0) -> None:
        """`_ledger_buzz`, never a reason for the phone leg to fail: the
        ledger observes the leg and never changes it."""
        try:
            self._ledger_buzz(pending, outcome, banner=banner,
                              idle_seconds=idle_seconds,
                              held_seconds=held_seconds)
        except Exception:
            logger.debug("buzz ledger line failed", exc_info=True)

    def _send_or_withhold(self, batch: list, idle, *, banner: bool,
                          held_seconds: float = 0.0) -> None:
        """S6 on a reading already taken: withhold every alert while the Mac
        was touched inside `MAC_PRESENT_SECONDS`, else push the batch as one
        buzz. `None` (the reading could not be taken) sends — a fault never
        silences the phone. Every alert gets its ledger line either way."""
        if not batch:
            return
        if idle is not None and idle < MAC_PRESENT_SECONDS:
            for alert in batch:
                self._ledger_quietly([alert], "withheld:mac_active",
                                     banner=banner, idle_seconds=idle,
                                     held_seconds=held_seconds)
            return
        outcome = "raised"
        try:
            outcome = self._push_phone_alerts(batch)
        except Exception:
            logger.warning("phone push failed", exc_info=True)
        self._ledger_quietly(batch, outcome, banner=banner, idle_seconds=idle,
                             held_seconds=held_seconds)

    async def _phone_leg(self, pending: list, *, banner: bool) -> None:
        """The phone's share of one drain, on the loop (S3, S6, S7).

        1. A `finished` alert is written down `withheld:finished` and goes
           no further; the phone's Needs you list and badge still show it.
        2. An alert whose session the phone's Needs you list does not show
           (`live_activity.listed_sessions`: a live row waiting, prompted
           or carded, or a prompt with no row — less the entries the person
           dismissed, `live_activity.shown_sessions`) is written down
           `withheld:unlisted` and goes no further — the phone would sweep
           it away at its next check-in (`clearDeliveredIfQuiet`). A stall or
           context warning on a busy agent stops here; a `security` row never
           does.
        3. Every other alert that is not `rule == "card"` — a permission
           ask, a security row, a signal on a listed session — meets the
           presence gate at once, so a permission ask is never delayed by
           a card's grace.
        4. A `card`-rule alert is held `PUSH_GRACE_SECONDS`, then dropped
           when its session holds no card any more (`dropped:card_gone`) or
           was muted (`dropped:muted`); the rest meet the presence gate. A
           grace of zero holds nothing.

        The presence reading is taken on the executor at the moment of
        sending, never on the loop. A cancelled leg (shutdown) writes
        nothing more.
        """
        loop = asyncio.get_running_loop()
        finished, rest = self._split_finished(pending)
        for alert in finished:
            self._ledger_quietly([alert], "withheld:finished", banner=banner)
        # Before any await: the phone's list as it stands at the drain, read
        # once; a held card alert is listed by construction.
        rest = self._withhold_unlisted(rest, banner=banner)
        grace = float(PUSH_GRACE_SECONDS)
        held, now = [], []
        for alert in rest:
            if grace > 0 and isinstance(alert, dict) \
                    and alert.get("rule") == "card":
                held.append(alert)
            else:
                now.append(alert)
        if now:
            idle = await self._mac_idle_on_executor(loop)
            self._send_or_withhold(now, idle, banner=banner)
        if not held:
            return
        await asyncio.sleep(grace)
        # A membership test, never a copy: the card carries the enrolment key.
        cards = getattr(self, "_active_notifications", None) or {}
        policy = self.__dict__.get("_alert_policy")
        survivors = []
        for alert in held:
            sid = str(alert.get("session_id") or "")
            if sid not in cards:
                self._ledger_quietly([alert], "dropped:card_gone",
                                     banner=banner, held_seconds=grace)
            elif policy is not None and policy.is_muted(sid):
                self._ledger_quietly([alert], "dropped:muted",
                                     banner=banner, held_seconds=grace)
            else:
                survivors.append(alert)
        if survivors:
            idle = await self._mac_idle_on_executor(loop)
            self._send_or_withhold(survivors, idle, banner=banner,
                                   held_seconds=grace)

    async def _cancel_phone_legs(self) -> None:
        """Cancel and await every phone leg in flight — `_shutdown`'s sweep,
        run before any store closes. A cancelled leg writes nothing."""
        for task in list(self.__dict__.get("_phone_leg_tasks") or ()):
            try:
                await self._cancel_and_wait(task)
            except Exception:
                logger.debug("phone leg ended badly at shutdown", exc_info=True)

    @staticmethod
    async def _mac_idle_on_executor(loop):
        """The S6 reading, off the loop: `Quartz` may block, the loop may
        not. Any failure reads `None`, which sends."""
        try:
            return await loop.run_in_executor(None, buzz_ledger.mac_idle_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Mac idle reading failed", exc_info=True)
            return None

    def _phone_leg_now(self, pending: list, *, banner: bool) -> None:
        """`_phone_leg` with no running loop — plain sync code in a test:
        the finished filter, the unlisted rung (`withheld:unlisted`) and
        the presence gate, read directly (there is no loop to block), and no
        grace, because there is nothing to wait on. `_push_phone_alerts`
        answers `no_loop` here as it always has."""
        finished, rest = self._split_finished(pending)
        for alert in finished:
            self._ledger_quietly([alert], "withheld:finished", banner=banner)
        rest = self._withhold_unlisted(rest, banner=banner)
        if not rest:
            return
        try:
            idle = buzz_ledger.mac_idle_seconds()
        except Exception:
            idle = None
        self._send_or_withhold(rest, idle, banner=banner)

    def _ledger_buzz(self, pending: list, outcome, *, banner: bool,
                     idle_seconds=None, held_seconds: float = 0.0) -> None:
        """One buzz-ledger line per alert just drained (`buzz_ledger.py`).

        Runs where `_deliver_alerts` runs — the loop, or plain sync code in
        a test — and does dict work only there: the alert's own row, the
        `_buzz_evidence` the decision attached, the waiting count the push
        carried as its badge, and the two loop-owned presence readings
        `_alert_suppressed` reads (`_frontmost_pids` / `_frontmost_at`,
        `_panel_on_screen()`). The `Quartz` idle probe and the file write
        run inside the executor callable, `_log_event`'s split: off the loop
        the append is made directly, or the ledger would be empty exactly
        where the tests look. Nothing here reads `title`, `body`,
        `subtitle`, `work` or the card.

        `idle_seconds` is the phone leg's own presence reading, handed in so
        the line and the gate agree; `None` probes here as before.
        `held_seconds` is how long the leg held the batch (S7), `0.0` for
        one sent at once. `collapsed` and `push_kind` describe the batch
        handed to this call — a withheld or dropped alert is its own batch
        of one.
        """
        ledger = getattr(self, "_buzz_ledger", None)
        if ledger is None or not pending:
            return
        outcome = outcome if isinstance(outcome, str) and outcome else "unknown"
        devices = 0
        if outcome.startswith("sent:"):
            try:
                devices = int(outcome[5:])
            except ValueError:
                devices = 0
        drain = secrets.token_hex(4)
        waiting = len((getattr(self, "_agents_snapshot_cache", None) or {})
                      .get("waiting") or [])
        push_kind = self._compose_push_kind(pending)
        try:
            held = max(0.0, float(held_seconds or 0.0))
        except (TypeError, ValueError):
            held = 0.0
        if idle_seconds is not None:
            try:
                idle_seconds = float(idle_seconds)
            except (TypeError, ValueError):
                idle_seconds = None
        now = time.time()
        pids = getattr(self, "_frontmost_pids", None) or set()
        fresh = bool(pids) and (
            now - float(getattr(self, "_frontmost_at", 0.0) or 0.0)
            <= FRONTMOST_TRUST_SECONDS)
        panel = bool(self._panel_on_screen(now))
        states = getattr(self, "_session_states", None) or {}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for alert in pending:
            if not isinstance(alert, dict):
                continue
            sid = str(alert.get("session_id") or "")
            pid = (states.get(sid) or {}).get("pid") if sid else None
            evidence = alert.get("_buzz_evidence")
            if not isinstance(evidence, dict):
                evidence = buzz_ledger.evidence(None, None, cooldown_gap=None)
            line = {
                "v": buzz_ledger.SCHEMA_VERSION,
                "id": "",
                "ts": now,
                "drain": drain,
                "alert_id": str(alert.get("id") or ""),
                "session_id": sid,
                "rule": str(alert.get("rule") or ""),
                "kind": str(alert.get("kind") or ""),
                "severity": str(alert.get("severity") or ""),
                "created_at": float(alert.get("created_at") or 0.0),
                "collapsed": len(pending),
                "push_kind": push_kind,
                "badge": waiting,
                "banner": bool(banner),
                "evidence": dict(evidence),
                "mac": {
                    "frontmost_fresh": fresh,
                    "frontmost_hit": bool(fresh and pid and pid in pids),
                    "panel_on_screen": panel,
                    "idle_seconds": None,
                },
                "phone": {"outcome": outcome, "devices": devices,
                          "held_seconds": held},
            }

            def write(line=line):
                if idle_seconds is None:
                    line["mac"]["idle_seconds"] = buzz_ledger.mac_idle_seconds()
                else:
                    line["mac"]["idle_seconds"] = idle_seconds
                ledger.append(line)

            if loop is None:
                write()
            else:
                loop.run_in_executor(None, write)

    @staticmethod
    def _compose_push_title(pending: list) -> str:
        """The buzz's first line, composed from nickname and kind. Beside it
        the payload carries the badge, the kind word, and — in `work`, from
        `_compose_push_work` — one short line naming the board card the agent
        was started for, or failing that the session's own title.

        The payload transits Vercel and Apple in plaintext (iOS renders the
        banner from the bare `aps` dict) and shows on a locked screen, so what
        rides here is a deliberate decision: the user relaxed the old
        nickname-and-count rule on 2026-09-06 to have the work named, with no
        preference key and no way to switch it off, and on 2026-09-20 added
        one line saying what is needed (`need`, from `_compose_push_need`) —
        the agent's own summary or question text, or a tool's bare name.
        What still never rides is the command or input preview, the tool's
        `description`, a file path, the project name and the branch. This
        helper in particular reads no key of the alert but `nickname` and
        `kind`; the kind is `Alert.kind`, decided once in
        `AlertPolicy.evaluate` and never re-derived here. The phone fetches the
        real content over the sealed channel when it is opened. Beside the
        words, a single-alert buzz carries its *subject* — the opaque session
        id and, where bound, the card id (`_compose_push_subject`) — so a
        phone with the Mac out of reach can open the card or agent from the
        picture it already holds: identifiers ride, words do not.
        """
        if len(pending) > 1:
            return f"{len(pending)} agents need you"
        alert = pending[0] if pending else {}
        nickname = str(alert.get("nickname") or "") or "An agent"
        kind = str(alert.get("kind") or "")
        if kind == alerting.KIND_SECURITY:
            # Not about an agent at all: the phone doors refused a burst of
            # knocks. No address rides here — the payload transits Apple in
            # plaintext — the phone reads the alert off the sealed channel.
            return "Somebody is knocking at Dark Army's door"
        if kind == "permission":
            return f"{nickname} wants to run a tool"
        if kind == "question":
            return f"{nickname} asked you a question"
        if kind == "finished":
            return f"{nickname} finished"
        return f"{nickname} needs you"

    @staticmethod
    def _compose_push_kind(pending: list) -> str:
        """The one kind word a buzz carries: the most urgent member of
        `alerts.KINDS` any pending alert names, so two agents collapsed into
        one buzz play the sound of the one that can least wait. Reads
        `kind` and nothing else; an unknown or missing kind is attention.
        """
        present = {str(alert.get("kind") or "") for alert in pending}
        for kind in alerting.KINDS:
            if kind in present:
                return kind
        return alerting.KIND_ATTENTION

    def _compose_push_work(self, pending: list) -> str:
        """What the agent is working on, for the buzz's second line — the
        title of the board card its session is bound to, else the session's
        own resolved title. `""` means *say nothing*, and the caller leaves
        the field off the wire entirely rather than sending an empty one.

        Three rules, in order. A **collapsed** buzz (more than one pending
        alert) gets nothing: the title already reads "N agents need you", and
        naming one of several agents' work would mislead on a glance. The
        **card** wins over the row's name, because it is what a person put on
        the board. A session with neither is nothing to say — `UNNAMED_SESSION`
        is the naming ladder's own "no name", not a name.

        Sources of truth, both already published and neither re-derived:
        `_card_titles_by_session()` (the last published board, rebound whole,
        so this loop-thread read is safe with no lock) and the agents
        snapshot's row `name` (`_session_name`'s answer). Deliberately **not**
        the row's `card_title` key, which is set only where the row is unnamed
        and whose meaning both clients consume.

        Reads no key of the alert but `session_id`: the alert's own words never
        travel.
        """
        if len(pending) != 1:
            return ""
        sid = str((pending[0] or {}).get("session_id") or "")
        if not sid:
            return ""
        return self._work_line_for_session(sid)

    @staticmethod
    def _compose_push_need(pending: list) -> str:
        """What is needed, for the buzz's third line — one sentence a person
        can act on from a locked screen. `""` means *say nothing*, and the
        caller leaves the field off the wire rather than sending an empty one.

        Reads exactly four keys of the one alert — `kind`, `rule`, `need`
        and `tool` — every one stamped at the gate by `AlertPolicy` and never
        re-derived here. A **collapsed** buzz (more than one pending alert)
        gets nothing, as `work` does. A **security** alert names no address.
        A **question** — the entry's dialog, a `bob-tldr` ask, or a relayed
        `AskUserQuestion`, which arrives as a `permission` rule with kind
        `question` and is tested first for that reason — is the question's
        own text, else a stock sentence. A **tool approval** is the tool's
        bare name and nothing of the command. Anything else (attention,
        finished) is the agent's own summary where it left one.

        Whitespace-collapsed and clamped to `relay_client.PUSH_NEED_CHARS`
        with a trailing ellipsis, `_work_line_for_session`'s shape.
        """
        if len(pending) != 1:
            return ""
        alert = pending[0] or {}
        kind = str(alert.get("kind") or "")
        rule = str(alert.get("rule") or "")
        need = str(alert.get("need") or "")
        tool = str(alert.get("tool") or "").strip()
        if kind == alerting.KIND_SECURITY:
            return ""
        if kind == "question":
            line = need or "Answer the question it asked"
        elif rule == "permission" and kind == "permission":
            line = f"Approve running {tool}" if tool else "Approve a tool call"
        else:
            line = need
        line = " ".join(line.split())
        if len(line) > relay_client.PUSH_NEED_CHARS:
            line = line[:relay_client.PUSH_NEED_CHARS - 1] + "\u2026"
        return line

    @staticmethod
    def _compose_push_face(pending: list) -> str:
        """The cast slug for one agent's portrait, or ``""`` when there is none.

        The caller leaves the field off the wire rather than sending an empty
        one. Only a single pending alert, only a kind in `alerts.FACE_KINDS`,
        and only a character already in `relay_client.PUSH_FACE_SLUGS`. Reads
        `kind` and `character` off the alert the policy stamped. Does not
        call `cast.character_for` again.
        """
        if len(pending) != 1:
            return ""
        alert = pending[0] or {}
        kind = str(alert.get("kind") or "")
        if kind not in alerting.FACE_KINDS:
            return ""
        character = str(alert.get("character") or "")
        if character not in relay_client.PUSH_FACE_SLUGS:
            return ""
        return character

    def _work_line_for_session(self, sid: str) -> str:
        """`_compose_push_work`'s per-session half — the card's title, else
        the row's own name, whitespace-collapsed and clamped — shared with
        the live card so the two legs never disagree about what to call
        the work. `""` means say nothing."""
        line = self._card_titles_by_session().get(sid) or ""
        if not line:
            for rows in (self._agents_snapshot_cache or {}).values():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("session_id") or "") != sid:
                        continue
                    name = str(row.get("name") or "")
                    if name and name != UNNAMED_SESSION:
                        line = name
                    break
                if line:
                    break
        line = " ".join(line.split())
        if len(line) > relay_client.PUSH_WORK_CHARS:
            line = line[:relay_client.PUSH_WORK_CHARS - 1] + "\u2026"
        return line

    def _push_phone_alerts(self, pending: list) -> str:
        """One buzz per drain to every token-holding phone, at exactly the
        banner moments — this rides `_deliver_alerts`' drain, so every rule
        upstream (transitions only, cooldowns, mutes, frontmost suppression)
        is inherited rather than reimplemented.

        Returns one word saying what became of the leg, for the buzz ledger
        and nothing else: `empty`, `push_off`, `remote_off`, `no_connector`,
        `no_loop`, `no_devices`, or `sent:<n>` with the number of phones a
        POST was created for. The body of the leg is unchanged by it.

        Gated three ways, all read at the moment of use: the `phone_push`
        preference, the away consent (`remote_access` — the push travels the
        internet, so it answers to the internet switch), and a built
        connector. The badge is the waiting bucket of the same snapshot the
        alerts were decided against, so the corner number and the banner
        never disagree, and the kind word comes from the same drain — the
        most urgent kind among the alerts collapsed into this one buzz.
        Composition is cheap dict work on the loop; the
        blocking POST runs on the connector's own executor inside
        `push_alert`, one detached task per device. A single-alert buzz also
        names its subject on every device — `session_id` and, where the
        session is bound to a card, `card_id` (`_compose_push_subject`),
        whatever the lock-screen switch says: identifiers ride, words do not.
        """
        if not pending:
            return "empty"
        if not getattr(self, "phone_push_enabled", True):
            return "push_off"
        if not getattr(self, "remote_access_enabled", False):
            return "remote_off"
        connector = getattr(self, "_relay_connector", None)
        if connector is None:
            return "no_connector"
        waiting = (self._agents_snapshot_cache or {}).get("waiting") or []
        body = {
            "title": self._compose_push_title(pending),
            "badge": len(waiting),
            "kind": self._compose_push_kind(pending),
        }
        # Absent, never empty: `push.js` would render `subtitle: ""` as a blank
        # second line, and a buzz with nothing to say must stay byte-identical
        # to the one this route sent before `work` existed.
        work = self._compose_push_work(pending)
        if work:
            body["work"] = work
        # Absent, never empty, for the same reason: a blank third line.
        need = self._compose_push_need(pending)
        if need:
            body["need"] = need
        # Absent, never empty, beside work and need: one cast slug, or nothing.
        face = self._compose_push_face(pending)
        if face:
            body["face"] = face
        # The subject: which agent, and which card, this buzz is about —
        # opaque ids the phone resolves against the picture it already
        # holds. Joined before the per-device loop, so every phone gets it
        # whatever its lock-screen switch says; a collapsed buzz and a
        # machine alert give `{}` and the body is byte-identical to before.
        body.update(self._compose_push_subject(pending))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "no_loop"
        # The lock-screen leg: one alert alone can be answered from a
        # banner, so a collapsed buzz carries nothing; a permission carries
        # its prompt's id and session, a question or attention alert its
        # session alone. Added per device below, and only where that
        # phone's switch is on — off, the body is byte-identical.
        act = self._compose_push_act(pending)
        sent = 0
        for device_id in relay.channel_ids():
            if relay.push_token(device_id) is None:
                continue
            sent += 1
            facts = dict(body)
            if act and relay.lock_screen_actions(device_id):
                facts.update(act)
            capture = getattr(self, "_decisions", None)
            if capture is None:
                loop.create_task(connector.push_alert(device_id, facts))
            else:
                async def send(did, facts):
                    receipt = await capture.call("receipt", did,
                        [identity for a in pending for identity in a.get("_decision_ids", [])]
                        if all(a.get("_decision_ids") for a in pending) else [])
                    if receipt:
                        facts.update(destination_version=1, receipt_id=receipt)
                    await connector.push_alert(did, facts)
                loop.create_task(send(device_id, facts))
        return f"sent:{sent}" if sent else "no_devices"

    def forget_live_activity(self, device_id: str) -> None:
        """A phone registered a fresh activity token (or cleared it): drop
        what this daemon remembers sending that device — the last landed
        state or the end mark, and the last refused body — so the next pass
        sends the standing state to the new activity rather than believing
        the old one is still up, already ended or still refusing. An
        in-flight POST is left to finish; its answer is discarded against
        the new registration (`_send_live_activity`). Called from the sealed
        verb; the dicts are the loop thread's, and the verb runs there."""
        did = str(device_id or "")
        self._live_activity_last.pop(did, None)
        self._live_activity_attempt.pop(did, None)
        self._live_activity_sent_at.pop(did, None)
        self._step_live_activity_generation(did)

    def _step_live_activity_generation(self, device_id: str) -> int:
        """Step the device's generation and return the new value — what a
        send captures, and what `forget_live_activity` moves past it."""
        generation = self._live_activity_generation.get(device_id, 0) + 1
        self._live_activity_generation[device_id] = generation
        return generation

    def _phone_listed_sessions(self) -> set[str] | None:
        """The session ids the phone's Needs you list shows right now, after
        its dismissals — the listed set less every dismissed entry, over the
        four inputs `_activity_subject` hands `subject` (the published
        buckets, the alert policy's prompt view, the sessions holding a
        *published* notification card, the last published board's cards)
        plus the ack store's own rows — the ones section ``inbox`` publishes
        to the phone, read directly so the leg never prunes the store.
        Re-derives nothing; loop thread, or plain sync code in a test. A
        daemon with no ack store gates on the listed set alone.

        ``None`` when an input cannot be read: the unlisted rung then gates
        nothing, S6's rule — a fault never silences the phone."""
        try:
            notified = [str(n.get("session_id") or "")
                        for n in self._notification_snapshot()]
            cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
            store = getattr(self, "_inbox_acks", None)
            acks = store.records() if store is not None else []
            listed = live_activity.shown_sessions(
                getattr(self, "_agents_snapshot_cache", None) or {},
                self._prompts_by_session(), notified=notified,
                cards=cards, acks=acks)
            self._listed_fault_logged = False
            return listed
        except Exception:
            # Loud once per fault, quiet while it persists.
            if self.__dict__.get("_listed_fault_logged"):
                logger.debug("phone Needs you list unreadable; not gating",
                             exc_info=True)
            else:
                logger.warning("phone Needs you list unreadable; not gating",
                               exc_info=True)
                self._listed_fault_logged = True
            return None

    def _activity_subject(self, snapshot: dict) -> dict | None:
        """`live_activity.subject` over the published buckets, the alert
        policy's own prompt view, the sessions holding a *published*
        notification card and the last published board's cards (the phone's
        one-entry rule needs the `needs_you` / `manual_check_due` card bound
        to a session). Re-derives nothing; loop thread only.

        The cards are `_notification_snapshot()`'s, not `_active_notifications`'
        keys: the snapshot is what the observers publish and what the phone
        reads as `snapshot.notifications` (`notifyIds`), and it *withholds* a
        "Waiting for input" card whose session is still inside the
        `WAITING_HYSTERESIS_SECONDS` window. Read off the raw dict, a
        stop-and-resume flicker inside that window admitted a row no surface
        listed and cost two pushes — an update and an end — out of the bucket
        the buzz shares, for a card the phone had never started."""
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        notified = [str(n.get("session_id") or "")
                    for n in self._notification_snapshot()]
        return live_activity.subject(
            snapshot or {}, self._prompts_by_session(),
            notified=notified, cards=cards)

    def _activity_content(self, subject: dict) -> dict:
        """The wire dict for one subject, `work` on `_compose_push_work`'s
        rule (the bound card's title, else the row's name, clamped)."""
        sid = str(subject.get("session_id") or "")
        return live_activity.content_state(
            subject, self._work_line_for_session(sid), time.time())

    def _activity_fleet_state(self, snapshot: dict) -> dict | None:
        """The shape-2 card, or None when the fleet is not up.

        Counts are `_activity_counts()` — the strip's own, not a second
        reading of the snapshot. Figures are `fleet_figures.compose` over
        the same published rows. The face is the subject's content state,
        or `empty_face` when nobody is waiting. Loop thread only, beside
        `_activity_subject`.
        """
        counts = self._activity_counts()
        if not live_activity.is_up(counts):
            return None
        # `fleet_face`, not `_activity_subject`: a waiter whose Needs-you
        # entry is a board card is counted in "need you", so it gets a face.
        cards = (getattr(self, "_board_state", None) or {}).get("cards") or []
        notified = [str(n.get("session_id") or "")
                    for n in self._notification_snapshot()]
        subject = live_activity.fleet_face(
            snapshot or {}, self._prompts_by_session(),
            notified=notified, cards=cards)
        face = (self._activity_content(subject) if subject
                else live_activity.empty_face(time.time()))
        return live_activity.fleet_state(
            counts,
            fleet_figures.compose(snapshot, getattr(self, "_burn_meter", None),
                                  time.time()),
            face)

    def _push_live_activity(self, snapshot: dict) -> None:
        """Keep every phone's Live Activity in step with the top of Needs you.

        `_push_phone_alerts`' three gates, read at the moment of use: the
        `phone_push` preference, the away consent and a built connector.
        Then per phone holding an activity token: the current subject's
        content state, compared with the last one this device was sent —
        unchanged sends nothing (`live_activity.same`, so a clock tick
        alone never costs a push); changed sends one ``update``; an empty
        list sends one ``end`` carrying the last state and, once it lands,
        the device is remembered as ``"ended"``.

        **``"ended"`` means no live token.** An end kills the activity's
        token on the phone, so nothing more is sent to that device — not
        even a new subject — until the phone registers a fresh token, which
        clears the mark (`forget_live_activity`). A device just (re)registered
        has no memory, so the first pass after registration sends an update.

        **A dead token ends the card.** Apple refusing the token (the
        mailbox's 502, `"dead"`) marks the device ``"ended"`` like a landed
        end: a different state would be refused just the same.

        **A refusal is remembered, not retried.** The last body sent and its
        outcome sit in `_live_activity_attempt`: the same body refused (a
        4xx, Apple's refusal of a dead token, an undeployed route) is not
        sent again until the picture changes; the same body unreachable (no
        answer, a 5xx) waits `LIVE_ACTIVITY_RETRY_SECONDS`. So an old mailbox
        or a dead token costs one token from the shared `_push_bucket` per
        distinct state, and the buzz beside it is never starved — the rule
        `docs/transport-contract.md` states under *The buzz has a live-card
        leg*. A body already in flight for this device is not sent twice.
        Cheap dict work on the loop; the POST runs on the connector's
        executor inside `push_activity`, one detached task per device,
        `_push_phone_alerts`' shape.
        """
        if not getattr(self, "phone_push_enabled", True):
            return
        if not getattr(self, "remote_access_enabled", False):
            return
        connector = getattr(self, "_relay_connector", None)
        if connector is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        subject = self._activity_subject(snapshot)
        shape1 = self._activity_content(subject) if subject else None
        for device_id in relay.channel_ids():
            if relay.activity_token(device_id) is None:
                continue
            # Shape 1 (an older phone, or an entry that never said) keeps
            # today's face-only card. Shape 2 is the fleet card. The fleet
            # state is read only for a shape-2 device — a shape-1 pass must
            # not pay for it, and must not send a face-less update.
            shape = relay.activity_shape(device_id)
            if shape == 2:
                state = self._activity_fleet_state(snapshot)
            else:
                state = shape1
            last = self._live_activity_last.get(device_id)
            if last == "ended":
                continue
            if state is None:
                body = {"event": "end"}
                if isinstance(last, dict):
                    body.update(last)
                remember = "ended"
            else:
                # A figures-only change waits out the interval; a count,
                # the face or the event falls through and sends at once.
                if (shape == 2 and isinstance(last, dict)
                        and live_activity.figures_only_change(state, last)):
                    sent_at = self._live_activity_sent_at.get(device_id)
                    if (sent_at is not None
                            and time.monotonic() - sent_at
                            < LIVE_ACTIVITY_FIGURES_INTERVAL_SECONDS):
                        continue
                if isinstance(last, dict) and live_activity.same(state, last):
                    continue
                body = {"event": "update", **state}
                remember = dict(state)
            if self._live_activity_body_pending(device_id, body):
                continue
            self._live_activity_inflight[device_id] = body
            loop.create_task(self._send_live_activity(
                connector, device_id, body, remember=remember,
                generation=self._step_live_activity_generation(device_id)))

    @staticmethod
    def _same_activity_body(a: dict | None, b: dict | None) -> bool:
        """Two composed bodies that would draw the same card: the same event
        and `live_activity.same` on the state (an end with no state is the
        same end)."""
        if not a or not b:
            return (not a) and (not b)
        if a.get("event") != b.get("event"):
            return False
        state_a = {k: v for k, v in a.items() if k != "event"}
        state_b = {k: v for k, v in b.items() if k != "event"}
        return live_activity.same(state_a, state_b)

    def _live_activity_body_pending(self, device_id: str, body: dict) -> bool:
        """Whether this body is already out, already refused, or unreachable
        too recently to try again — the three reasons a pass sends nothing
        although the picture and the memory disagree."""
        inflight = self._live_activity_inflight.get(device_id)
        if inflight is not None and self._same_activity_body(inflight, body):
            return True
        attempt = self._live_activity_attempt.get(device_id)
        if attempt is None:
            return False
        sent, outcome, at = attempt
        if not self._same_activity_body(sent, body):
            return False
        if outcome == "refused":
            return True
        if outcome == "unreachable":
            return time.monotonic() - at < LIVE_ACTIVITY_RETRY_SECONDS
        return False

    async def _send_live_activity(self, connector, device_id: str,
                                  body: dict, *, remember,
                                  generation: int) -> None:
        """One detached send. Landed: the state (or the end mark) is what
        this device is remembered as holding, and any refusal is forgotten.
        Refused or unreachable: the body and the verdict are remembered so
        `_live_activity_body_pending` bounds the retry. Skipped (no token
        any more, the bucket empty, a shape the connector would not send):
        nothing is remembered and the next pass decides afresh. A send that
        raises is unreachable. The in-flight mark is cleared whatever
        happened. ``generation`` is the device's counter as this send was
        created: a fresh registration during the flight
        (`forget_live_activity`) or a newer send steps it, and the answer
        is then discarded — an old `end` landing after the phone registered
        a new activity's token would otherwise mark the new activity
        "ended" and its card would never update again."""
        try:
            outcome = await connector.push_activity_outcome(device_id, body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("live card push failed", exc_info=True)
            outcome = "unreachable"
        finally:
            if self._live_activity_inflight.get(device_id) is body:
                self._live_activity_inflight.pop(device_id, None)
        if self._live_activity_generation.get(device_id) != generation:
            return
        if outcome == "landed":
            self._live_activity_last[device_id] = remember
            self._live_activity_attempt.pop(device_id, None)
            self._live_activity_sent_at[device_id] = time.monotonic()
        elif outcome == "dead":
            # Apple refused the token itself (iOS ends an activity at eight
            # hours): every later state would be refused too, and the fleet
            # figures change each minute, so it is treated as an end.
            self._live_activity_last[device_id] = "ended"
            self._live_activity_attempt.pop(device_id, None)
        elif outcome in ("refused", "unreachable"):
            self._live_activity_attempt[device_id] = (
                dict(body), outcome, time.monotonic())

    @staticmethod
    def _compose_push_act(pending: list) -> dict:
        """What a phone may press on this buzz, or `{}`.

        Exactly one pending alert, or nothing: the buttons act on one
        subject. A `permission` rule carries `act: "permission"` with
        `request_id` and `session_id` (Allow / Deny → `permission_verdict`);
        a question or attention kind carries `act: "acknowledge"` with the
        session (→ `dismiss`); a machine alert (no session) and a finish
        carry nothing. Identifiers only — the question's words never ride.
        """
        if len(pending) != 1:
            return {}
        alert = pending[0]
        sid = str(alert.get("session_id") or "")
        if not sid:
            return {}
        if str(alert.get("rule") or "") == "permission":
            if not alert.get("answerable", True):
                return {}
            rid = str(alert.get("request_id") or "")
            if not rid:
                return {}
            return {"act": "permission", "request_id": rid, "session_id": sid}
        kind = str(alert.get("kind") or "")
        if kind in ("question", alerting.KIND_ATTENTION):
            return {"act": "acknowledge", "session_id": sid}
        return {}

    def _compose_push_subject(self, pending: list) -> dict:
        """Which agent, and which card, this buzz is about — or `{}`.

        Exactly one pending alert with a non-empty `session_id` gives
        `{"session_id": sid}` plus `"card_id"` where the last published board
        binds that session to a card (`_card_ids_by_session`, the rule
        `_compose_push_work` reads the title by). A collapsed buzz, a machine
        alert (`security`, empty session) and an unknown shape give `{}`, so
        those bodies stay byte-identical to before the subject existed.

        Identifiers only: the same opaque session id the act leg already
        carried, and a card id — no words, no key, no digest. The phone
        resolves both against the picture it holds and re-derives nothing;
        `relay_client.push_alert` joins each only in `PUSH_ID_SHAPE`.
        """
        if len(pending) != 1:
            return {}
        alert = pending[0] if isinstance(pending[0], dict) else {}
        sid = str(alert.get("session_id") or "")
        if not sid:
            return {}
        subject = {"session_id": sid}
        cid = self._card_ids_by_session().get(sid) or ""
        if cid:
            subject["card_id"] = cid
        return subject

    def set_phone_push(self, enabled: bool) -> None:
        """Buzz the paired phone about raised alerts, or stop. Thread-safe:
        a plain bool the drain reads at the moment of use — no loop hop,
        nothing to start or stop, exactly `notification_sound`'s shape."""
        self.phone_push_enabled = bool(enabled)

    def mute_session_alerts(self, session_id: str) -> None:
        """Silence one agent's interruptions for as long as it is around.

        Per session, because the agent you have decided to ignore is rarely the
        whole fleet. Held by the policy, which forgets it when the session
        vanishes — muting is not a preference, it is a decision about one run."""
        policy = self.__dict__.get("_alert_policy")
        if policy is not None:
            policy.mute(session_id)

    async def _transcript_scanner(self) -> None:
        """Backfill per-turn token detail from Claude Code's transcripts.

        Runs immediately and then on a slow timer, always in the executor: the
        first pass over a long history is seconds of blocking file I/O, and the
        API must be answering before it finishes rather than after."""
        while self._running:
            if self._history is not None:
                try:
                    loop = asyncio.get_running_loop()
                    summary = await loop.run_in_executor(
                        None, transcript_scan.scan, self._history
                    )
                    if summary.get("turns_added"):
                        logger.info("Transcript backfill: %s", summary)
                    grok = await loop.run_in_executor(
                        None, grok_scan.scan, self._history
                    )
                    if grok.get("turns_added"):
                        logger.info("Grok history backfill: %s", grok)
                    # Codex's journals, after Grok, in the same executor hop
                    # pattern: History's token cost reads their turns. A
                    # bounded pass that stopped early is remembered, so the
                    # report can say Codex is still being read rather than
                    # publish a short read as the whole period.
                    codex = await loop.run_in_executor(
                        None, codex_history.scan, self._history
                    )
                    self._codex_history_partial = bool(codex.get("partial"))
                except Exception:
                    logger.warning("transcript scan failed", exc_info=True)
            await asyncio.sleep(TRANSCRIPT_SCAN_INTERVAL)

    async def _history_pruner(self) -> None:
        """Age out turn and sample rows on a slow timer. Sessions and state events
        are kept: they are small, and they are the narrative you look back on."""
        while self._running:
            await asyncio.sleep(HISTORY_PRUNE_INTERVAL)
            if self._history is None:
                continue
            removed = await asyncio.get_running_loop().run_in_executor(
                None, self._history.prune
            )
            if any(removed.values()):
                logger.info("History pruned: %s", removed)

    async def _snapshot_refresher(self) -> None:
        """Re-push the agent snapshot on a slow tick.

        Pushes are otherwise driven by structural state changes, which leaves two
        things frozen: a session that only just earned a title keeps whatever
        placeholder it was given, and every "waiting 3m" stops counting. Cheap by
        construction — transcript parses are mtime-memoised and paths are cached,
        so a tick where nothing changed re-reads nothing."""
        while self._running:
            await asyncio.sleep(SNAPSHOT_REFRESH_SECONDS)
            if self._observers_implementing("on_agents_change"):
                self._schedule_agents_push()

    def _drain_pending_agents_push(self) -> None:
        """Run one trailing push if state changed while the last one was in flight.
        Bounded: the flag is cleared before the re-run, so a steady stream of events
        produces a steady stream of pushes, not a growing pile of them."""
        if not self._agents_push_pending:
            return
        self._agents_push_pending = False
        self._agents_push_task = asyncio.ensure_future(self._push_agents_snapshot())

    def _schedule_display_push(self) -> None:
        """Push the slot list from a sync caller (the reconciler callback).

        A reconciler tick moves more than the counts now: a background agent that
        appears, blocks or goes quiet changes who is on stage, and without this its
        Dark Army would only show up when some *other* session happened to fire a hook.
        Outside a running loop (unit tests construct daemons freely) there is
        nothing connected to push to."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._wake_surfaces())

    async def _wake_surfaces(self) -> None:
        """Tell the menu-bar strip and the panel that something moved.

        This used to also compute an animation per session and write it to the
        transports. There are no transports — the pixel-art window was the only
        one — so that half was computing a decision on every hook event, logging
        it, and discarding it.
        """
        self._notify_activity()
        self._schedule_agents_push()

    #: Seconds between two passes of each background sweep.
    SWEEP_INTERVAL_SECONDS = 30

    async def _sweep_forever(self, sweep, name: str) -> None:
        """Call `sweep` every `SWEEP_INTERVAL_SECONDS` while the daemon runs,
        then tell the surfaces about any card it took. `sweep` answers
        whether the surfaces need waking. Nothing awaits these tasks, so a
        failure is logged and the next pass still comes; one escaping
        exception would otherwise end that sweep for the life of the process
        with no sign of it."""
        while self._running:
            await asyncio.sleep(self.SWEEP_INTERVAL_SECONDS)
            try:
                had_cards = set(self._active_notifications)
                wake = sweep()
                await self._dismiss_evicted_cards(had_cards)
                if wake:
                    await self._wake_surfaces()
            except Exception:
                logger.warning("The %s sweep failed; trying again next pass", name,
                               exc_info=True)

    async def _staleness_checker(self) -> None:
        def evict():
            self._evict_stale_sessions()
            return True
        await self._sweep_forever(evict, "staleness")

    def _check_liveness(self) -> list[str]:
        """One liveness pass, done synchronously so a test can drive it.
        Answers the ids it forgot, in the order they went."""
        dead = []
        terminal_gone = []
        clock = time.monotonic()
        # Grok leftovers live in the roster, not hook state. Sweep first so a
        # departed file entry is forgotten here rather than waiting for the
        # next snapshot that happens to call `_reconciled_categories`.
        self._refresh_grok_records()
        hostless = set(self._hostless_hosted_ids())
        for sid, state in list(self._session_states.items()):
            pid = self._ensure_session_pid(sid, state)
            # A Grok hook stamps the shared leader. That process is alive,
            # but it is not this session — `_still_our_process` is False
            # because `looks_like_grok` excludes it. Treating that as
            # death evicted live interviews every 30s while the leader was
            # still running. A recycled pid that now names Dock still
            # evicts, below. The pidless path applies the grace / waiting
            # exemption instead.
            if (state.get("provider") == "grok"
                    and _is_live_grok_leader(pid)):
                pid = None
            if pid is None:
                # No PID = pre-PID hook and no event since (a current hook event
                # would backfill it). If also silent past the grace window it is a
                # dead ghost. Sessions in `categorize()`'s waiting bucket — a card
                # held, or `error` / `waiting` — go quiet legitimately
                # (blocked on the human) and are exempt from the short grace — but
                # not immortal: `_evict_stale_sessions` spares the same bucket, so
                # a pidless waiter had *no* eviction path at all and sat under
                # "Needs you" for ever. With no PID there is no liveness check
                # left to catch it, so past the wall-clock staleness timeout —
                # the same window a pidful waiter gets, via the identity check
                # below — it goes.
                # A hosted session has a better witness than silence: the
                # broker's own terminal list. Its process died with the
                # terminal, so it goes now, waiter or not.
                if sid in hostless:
                    terminal_gone.append(sid)
                    continue
                idle = clock - state.get("last_event_monotonic", clock)
                if self._waiting_on_human(sid, state):
                    if idle > self._session_staleness_timeout:
                        dead.append(sid)
                elif idle > PIDLESS_GRACE_SECONDS:
                    # The same witness the roster pass accepts: a Grok
                    # turn running on with its tab gone is quiet on the
                    # hook side and busy on disk.
                    if not self._grok_headless_turn_alive(
                            sid, state, time.time()):
                        dead.append(sid)
                continue
            # Not a bare pid_exists: that only proves *something* holds the number.
            # A recycled PID reads as alive forever, and 'waiting' sessions are
            # exempt from wall-clock staleness precisely because this check is
            # supposed to be the one that catches them (see _evict_stale_sessions)
            # — so a waiting session whose PID got reused became un-evictable, and
            # stop_session refuses it too, leaving a row nothing on screen can
            # clear. Same identity rule as the signal path, for the same reason.
            if not _still_our_process(pid, state.get("provider")):
                dead.append(sid)
        for sid in dead:
            pid = self._session_states[sid].get("pid")
            logger.info(
                "Liveness: evicting session %s (%s)",
                sid[:12],
                f"PID {pid} gone" if pid is not None else "no PID, silent past grace",
            )
            self._forget_session(sid, "no process" if pid is not None else "evicted")
        for sid in terminal_gone:
            logger.info("Liveness: evicting session %s (its hosted terminal "
                        "is gone)", sid[:12])
            self._forget_session(sid, TERMINAL_CLOSED_REASON)
        if dead or terminal_gone:
            self._persist_sessions()
        return dead + terminal_gone

    def _note_closed(
        self,
        sid: str,
        *,
        pid: Optional[int] = None,
        create_time: Optional[float] = None,
    ) -> None:
        """Remember that Dark Army ended this run by closing its terminal.

        The first pid/create_time we store for a sid sticks: a later settle
        must not replace the closed process with a sibling TUI that
        `load_recent` attached in the same cwd.
        """
        if not sid:
            return
        prev = self._closed_ids.get(sid)
        if prev is not None:
            if prev[1] is not None:
                pid = prev[1]
            if prev[2] is not None:
                create_time = prev[2]
        self._closed_ids[sid] = (time.monotonic(), pid, create_time)

    def _closed_by_bob(self, sid: str) -> bool:
        return sid in self._closed_ids

    def _codex_thread_resumed_after_close(
        self, record: codex_rollouts.CodexRecord,
    ) -> bool:
        """True when this closed Codex thread is a different process than Dark Army closed.

        cwd-level ``process_seen`` is not a resume. Admit only a new
        pid/create_time on this record, or an ``explicit_resume`` of this
        thread-id that is not the identity just closed.
        """
        note = self._closed_ids.get(record.session_id)
        if note is None:
            return False
        closed_turn = self._closed_codex_turns.get(record.session_id)
        if closed_turn is not None:
            # A shared-server thread: the server outlives the terminal, so
            # only a turn after the one closed says the person came back.
            return bool(record.turn_id) and record.turn_id != closed_turn
        _, closed_pid, closed_created = note
        ident = record.process_identity
        if ident is None:
            return False
        if closed_pid is not None and ident.pid == closed_pid:
            return False
        if (
            closed_pid is None
            and closed_created is not None
            and abs(ident.create_time - closed_created) <= 0.01
        ):
            return False
        if ident.match_kind == "explicit_resume":
            return True
        if closed_pid is not None or closed_created is not None:
            return True
        return False

    def _forget_session(self, sid: str, reason: str = "") -> None:
        """Drop every trace of a hook-tracked session. Shared by every removal path
        — they have to forget exactly the same things.

        `reason` is the end verdict: pass it and the session leaves a tombstone in
        `_finished` (see _record_finished). Only a caller that removed the session
        for good should pass one.

        The Grok suppress-list is stamped from the *reason*, not from every
        removal. `_grok_ended` says "Grok itself is done with this id", and
        `_live_grok_records` skips a stamped id for as long as the roster still
        lists it — so stamping a session that merely went quiet past the
        staleness timeout hid a tab that was alive and in `active_sessions.json`,
        permanently. Wall-clock eviction is a statement about *our* bookkeeping,
        not about the tab, and a genuinely dead one is caught by the pid check in
        `_live_grok_records` anyway."""
        self._refinement_receipts.pop(sid, None)
        self._grok_origin_probed.pop(sid, None)
        state = self._session_states.pop(sid, None)
        is_grok = (state is not None and state.get("provider") == "grok") or sid in self._grok_records
        if is_grok and reason in GROK_END_REASONS:
            self._grok_ended.add(sid)
        self._active_notifications.pop(sid, None)
        self._low_priority_sent.pop(sid, None)
        # Both are keyed by session id and neither had a removal path, so a daemon
        # running for weeks kept one entry per session it had ever seen. This is
        # the single funnel every removal goes through, so it is the place they
        # belong — the alternative is a sweep that has to re-derive what is dead.
        self._ai_title_cache.pop(sid, None)
        self._last_metric_sample.pop(sid, None)
        self._pending_questions.pop(sid, None)
        # A Dark Army-owned terminal outlives the session id it was named for:
        # `/clear` ends this id and starts the next one in the same process.
        # Unnaming it here lets the next enrich pass name the successor
        # through `_pty_handle_for(sid, stub)` — still behind
        # `_bindable_candidate`, so a background agent cannot take it.
        # `getattr`, like `_decisions` below: half-built daemons in tests.
        pty = getattr(self, "_pty", None)
        if pty is not None:
            pty.unbind(sid)
        capture = getattr(self, "_decisions", None)
        if capture is not None:
            capture.question_clear(sid, forget=True)
        # A prompt outlives the session it blocked only as a row nobody can
        # answer. `_reap_permissions` would get it once the channel expired;
        # this is the same fact known sooner and for certain.
        for rid in [r for r, row in self._permission_requests.items()
                    if (row.get("session_id") or "") == sid]:
            row = self._permission_requests.pop(rid, None)
            # A staged verdict was logged when it was staged; only a prompt
            # that ended with no answer is a lapse.
            if row is not None and not row.get("verdict"):
                self._log_permission(row, "permission_resolved",
                                     outcome="lapsed", why="the session ended")
        if reason and state is not None:
            # The diary's finish line rides inside `_record_finished` — the
            # seam every finish shares — so it is written there, once.
            self._record_finished(sid, state, reason)
        elif reason:
            # No hook state to tombstone, but the enrich loop may still have
            # logged this id's start (a roster stub Dark Army was told to forget).
            self._log_session_end(sid, reason, state or {})
        # An empty `reason` is a bookkeeping eviction, not a finish: the start
        # stays on record so a returning id logs no second one, and a later
        # real end through `_record_finished` pairs it.
        self._grok_records.pop(sid, None)
        store = getattr(self, "_inbox_acks", None)
        if store is not None:
            store.forget_session(sid)

    def _record_finished(self, sid: str, state: dict, reason: str) -> None:
        """Freeze what we know about a session that just stopped, so the panel can
        keep showing it for FINISHED_RETENTION_SECONDS.

        The statusline metrics (cost, context, budget) are not copied in here: they
        live in `_session_metrics`, which _collect_agent_stubs garbage-collects the
        moment a session leaves the categories, and that sweep now spares anything
        tombstoned. One copy, expired by the same clock as the row it belongs to.

        The *time*, though, has to be captured: it is `last_event`, not now — for a
        quiet session that is when it went idle, and
        for one killed mid-tool it is the last thing it did. Stamping the removal
        instead would restart the clock at eviction, so a row that had been reading
        "finished 4m ago" would snap back to zero the instant the evictor ran.

        The transcript half is deliberately not frozen: it is still on disk with a
        frozen mtime, so StatsCache keeps resolving it and the token counts go on
        *finalising* as the transcript scanner catches up.

        The *place* is frozen too, resolved below by the same session-place
        ladder every other surface reads — the enriched workspace label and
        cwd of the row the session wore while it lived —
        so a tombstone keeps the same project name its live row had, even for
        the providers whose runs leave no transcript behind (a Grok or Codex
        tombstone used to fall to the raw hook-time basename and could sit on
        a wrongly named tab for the whole retention window). The cwd goes on
        the stub so `_enrich_agent_stubs`' workspace ladder runs on tombstones
        of every provider; the known cost is that a tombstone naming an
        un-enrolled folder is now swept by the enrolment filter, which is the
        direction un-enrolment already points for live rows."""
        rec = self._agent_records.get(sid)
        subs = state.get("subagents") or set()
        place_cwd, place_project = self._session_place(sid)
        self._finished[sid] = {
            "finished_at": state.get("last_event", time.time()),
            "finished_mono": time.monotonic(),
            "end_reason": reason,
            "stub": {
                "session_id": sid,
                "project": place_project or state.get("project", "")
                or (rec.project if rec else ""),
                "cwd": place_cwd or state.get("cwd")
                or (rec.cwd if rec else ""),
                "state": state.get("state"),
                "subagents": len(subs),
                "subagent_ids": sorted(subs),
                "pid": state.get("pid") or (rec.pid if rec else None),
                "current_tool": state.get("tool_name", ""),
                "kind": rec.kind if rec else "interactive",
                "agent_activity": rec.activity if rec else "",
                "cli_name": rec.name if rec else "",
                "provider": state.get("provider") or (
                    "grok" if sid in self._grok_records else "claude"
                ),
            },
        }
        stub = self._finished[sid]["stub"]
        # Exact already-published registry identity; never an inbox or nickname.
        published = [row for group in getattr(self, "_agents_snapshot_cache", {}).values()
                     for row in group if row.get("session_id") == sid
                     and row.get("provider", "claude") == stub["provider"]]
        addresses = {row.get("address") for row in published if row.get("address")}
        if len(addresses) == 1:
            stub["retained_address"] = addresses.pop()
            stub["retained_address_source"] = "published_registry"
        # Where the session came from, carried onto the tombstone. `origin`
        # lives in `_session_states`, which `_forget_session` pops, so without
        # this copy a row that said "Dark Army started this" stopped saying it
        # the moment it moved to *Recently finished* — the one place a person
        # goes to ask what that thing was. Stripped out of the published entry
        # in `_enrich_agent_stubs`: the raw stamp is a key, not a caption.
        if state.get("origin"):
            stub["origin"] = state["origin"]
        # Same reason as origin: `_forget_session` pops the state, and the
        # finished row still has to say the tab was gone.
        if state.get("tab_gone"):
            stub["tab_gone"] = True
        if state.get("_codex_stats") is not None:
            stub["_codex_stats"] = state["_codex_stats"]
        if state.get("_codex_metrics") is not None:
            stub["_codex_metrics"] = state["_codex_metrics"]
        if stub["provider"] == "codex":
            # These reports are already bounded by rollout aggregation. Freeze
            # their own dictionaries so closing the parent preserves its reads.
            stub["review_reports"] = [dict(report) for report in state.get("review_reports", [])]
            stub["review_reports_omitted"] = state.get("review_reports_omitted", 0)
        self._prune_finished()
        # The diary's finish line, here because this is the seam **every**
        # finish shares — `_forget_session`, the roster vanish in
        # `_on_agent_records`, the Codex settle and refresh, the Grok end,
        # `stop_session`'s non-hook legs and the terminal close. Logging it
        # in `_forget_session` alone left every Codex, Grok-roster and
        # background row with a start and no finish, and its id parked in
        # `_logged_starts` for good. After the tombstone is written, so the
        # project it reads is the one the row wore.
        self._log_session_end(sid, reason, state)

    def _log_session_end(self, sid: str, reason: str, state: dict) -> None:
        """One `session_end` line for a session whose start the diary wrote,
        and pop the id so a returning session logs a fresh start. Exactly
        once per finish: the pop is the guard, so a second caller for the
        same end finds nothing to log. A session whose start was never
        logged (restored from `sessions.json`, never appeared live) has no
        start to pair a finish with and gets none."""
        logged_starts = getattr(self, "_logged_starts", None)
        if not reason or logged_starts is None or sid not in logged_starts:
            return
        logged_starts.discard(sid)
        detail: dict = {"end_reason": reason}
        finished_at = float((self._finished.get(sid) or {}).get("finished_at")
                            or (state or {}).get("last_event") or time.time())
        started = getattr(self, "_starts_shown", {}).get(sid)
        if started:
            detail["duration_seconds"] = max(0.0, finished_at - started)
        cost = (self._session_metrics.get(sid) or {}).get("cost_usd")
        if isinstance(cost, (int, float)):
            detail["cost"] = float(cost)
        self._log_session_event(sid, "session_end", **detail)

    def _seed_logged_starts(self) -> None:
        """Rebuild `_logged_starts` from the diary on open, so a restart does
        not write a false start for every session still on the fleet. A
        session counts as started when its newest start-or-end entry is a
        `session_start`. Called from `run()` right after the diary opens; a
        daemon without a diary keeps an empty set."""
        log = getattr(self, "_event_log", None)
        if log is None:
            return
        seen: set[str] = set()
        started: set[str] = set()
        for entry in log.recent():            # newest first
            kind = entry.get("kind")
            if kind not in ("session_start", "session_end"):
                continue
            sid = str(entry.get("session_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            if kind == "session_start":
                started.add(sid)
        self._logged_starts = started

    def _prune_finished(self) -> None:
        """Expire tombstones past the retention window, newest kept first.

        Monotonic, for the same reason eviction is: a macOS sleep/wake must not
        empty the section wholesale. The wall-clock `finished_at` is only ever used
        for the label."""
        now_mono = time.monotonic()
        for sid, record in list(self._finished.items()):
            if now_mono - record["finished_mono"] > FINISHED_RETENTION_SECONDS:
                del self._finished[sid]
        for sid, note in list(self._closed_ids.items()):
            if now_mono - note[0] > FINISHED_RETENTION_SECONDS:
                del self._closed_ids[sid]
                self._closed_codex_turns.pop(sid, None)
        if len(self._finished) > MAX_FINISHED_RECORDS:
            keep = sorted(
                self._finished.items(), key=lambda kv: kv[1]["finished_mono"], reverse=True
            )[:MAX_FINISHED_RECORDS]
            self._finished = dict(keep)

    async def _liveness_checker(self) -> None:
        """Forget, every pass, the sessions whose process is gone
        (`_check_liveness`); the surfaces wake only when one went."""
        await self._sweep_forever(self._check_liveness, "liveness")

    def set_session_timeout(self, seconds: int) -> None:
        """How long a quiet session stays listed.

        ``<= 0`` is the old picker's Never. That picker is gone, and a
        timeout of 0 would drop every non-waiting session on the next
        tick, so it becomes the default. A stored 600 or 1800 still wins.
        """
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            value = int(DEFAULT_SESSION_STALENESS_SECONDS)
        self._session_staleness_timeout = float(value)
        logger.info("Session staleness timeout set to %ds", value)

    def set_lan_access(self, enabled: bool) -> None:
        """Open or shut the phone listener. Thread-safe.

        Stashes the flag always. If the daemon loop is running, schedules
        ``start_lan`` / ``stop_lan`` onto it; if it is not (startup, before
        ``run()``), ``run()`` starts the listener after the loopback API
        binds, so a failed LAN bind cannot take the panel down with it.
        """
        self.lan_access_enabled = bool(enabled)
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _apply():
            if self.lan_access_enabled:
                await self._api.start_lan()
            else:
                await self._api.stop_lan()
            self._api._broadcast()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_apply())
        else:
            asyncio.run_coroutine_threadsafe(_apply(), loop)

    def set_remote_access(self, enabled: bool) -> None:
        """Open or shut the away path. Thread-safe, `set_lan_access`'s shape.

        Stashes the flag always; if the loop is running, schedules the
        connector's start/stop onto it. Before `run()`, the flag is applied
        after the loopback API binds, so a broken relay setup cannot take
        the panel down with it. Nothing here listens: the connector only
        ever reaches *out* to the mailbox.
        """
        self.remote_access_enabled = bool(enabled)
        self._remote_access_generation += 1
        generation = self._remote_access_generation
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _apply():
            if self.remote_access_enabled:
                self._start_relay_connector()
                if self.relay_ws_enabled:
                    self._start_relay_socket()
            else:
                # Away off stops both lanes: the socket rides the away
                # consent and never outlives it. The applies queue FIFO,
                # so a quick off→on lets the on-apply run while this one
                # awaits the socket's stop; the latest stash wins — bail
                # rather than stop the mailbox connector it just started.
                await self._stop_relay_socket()
                if generation != self._remote_access_generation:
                    return
                await self._stop_relay_connector()
                if generation != self._remote_access_generation:
                    return
            self._api._broadcast()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_apply())
        else:
            asyncio.run_coroutine_threadsafe(_apply(), loop)

    def set_relay_ws(self, enabled: bool) -> None:
        """Open or shut the away link's socket lane. Thread-safe,
        `set_remote_access`'s shape: stash the flag always; if the loop is
        running, schedule the socket connector's start/stop onto it. The
        connector is started only while `remote_access_enabled` is on too
        — the socket is a second way through the same consent, never a
        way round it — and blocks nothing on the caller's thread."""
        self.relay_ws_enabled = bool(enabled)
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _apply():
            if self.relay_ws_enabled and self.remote_access_enabled:
                self._start_relay_socket()
            else:
                await self._stop_relay_socket()
            self._api._broadcast()

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(_apply())
        else:
            asyncio.run_coroutine_threadsafe(_apply(), loop)

    def _start_relay_socket(self) -> None:
        """On the loop. Builds the socket connector once, hangs its `nudge`
        on `ApiServer.on_picture` so every changed picture reaches an armed
        phone, and starts its tasks."""
        if self._relay_socket is None:
            self._relay_socket = relay_ws.RelaySocketConnector(self._api)
        self._api.on_picture = self._relay_socket.nudge
        self._relay_socket.start()

    async def _stop_relay_socket(self) -> None:
        if self._relay_socket is not None:
            self._api.on_picture = None
            await self._relay_socket.stop()

    def _start_relay_connector(self) -> None:
        """On the loop. Builds the connector once and starts its tasks."""
        if self._relay_connector is None:
            self._relay_connector = relay_client.RelayConnector(self._api)
        self._relay_connector.start()

    async def _stop_relay_connector(self) -> None:
        if self._relay_connector is not None:
            await self._relay_connector.stop()

    def record_remote_action(self, device_id: str, action: str,
                             ok: bool) -> None:
        """One write driven from away, for the panel's "Done remotely" list.
        Bounded by the deque; never a key, a token or a payload."""
        self._remote_activity.append({
            "device_id": str(device_id or ""),
            "action": str(action or ""),
            "at": time.time(),
            "ok": bool(ok),
        })

    def _pairing_hosts(self) -> tuple[list[str], str]:
        """``(candidates, refusal)`` — every address a phone could try, best
        first, and the daemon's own sentence when there is no usable one.

        Both come from one reading of the interface list, so the list and the
        refusal can never disagree. All the judgment lives in `lan_hosts`;
        this is the seam that supplies it with the machine.
        """
        addrs = lan_hosts.live_addrs()
        hosts = lan_hosts.candidates(
            addrs, lan_hosts.default_route_addr(), socket.gethostname())
        return hosts, lan_hosts.refusal(addrs)

    #: How long a reading of this Mac's own addresses is believed. `state()`
    #: is built once per SSE frame — several times a second on a busy fleet —
    #: and `live_addrs()` enumerates every interface, so the reading is taken
    #: on a clock rather than per frame. Well under the phone's own minute
    #: between full home walks, so a DHCP move is still noticed within one.
    PAIRING_HOSTS_TTL = 30.0

    def _current_lan_hosts(self) -> list[str]:
        """This Mac's addresses **as they are now**, for the snapshot.

        The pairing QR's `hosts` are a photograph taken once, and the phone
        keeps them for the life of the pairing — so a Mac that moved to a new
        address (a DHCP lease renewed, a different Wi-Fi, Ethernet instead of
        Wi-Fi) became unreachable at home for ever, with the relay still
        answering. The phone then sat in AWAY on its own sofa. Publishing the
        live list lets a phone that can only hear the relay learn where home
        went, and it is exactly the reading `begin_pairing` already trusts.

        Not a secret: a phone on this Wi-Fi can see these addresses by
        looking, and away it reaches the phone sealed end to end, so the
        relay learns nothing. Never an address that is not this machine's,
        and never a key, digest or token.
        """
        now = time.monotonic()
        cached = self._lan_hosts_cache
        if cached is not None and now - cached[0] < self.PAIRING_HOSTS_TTL:
            return list(cached[1])
        try:
            hosts, _refusal = self._pairing_hosts()
        except Exception:  # pragma: no cover - an unreadable interface list
            logger.debug("lan hosts: reading failed", exc_info=True)
            hosts = list(cached[1]) if cached else []
        self._lan_hosts_cache = (now, list(hosts))
        return list(hosts)

    def begin_pairing(self, *, allow_typed: bool = False) -> dict:
        """Mint a two-minute pairing code. Refuses while the LAN door is shut
        — a QR for a door that is shut would pair nothing — and refuses again
        when this Mac has no address a phone could reach, rather than showing
        a code that cannot work.

        The address check is **before** `devices.begin()`: minting resets the
        active code and starts its two-minute window, so a press that only
        raises an alert must not burn one.

        ``allow_typed`` arms the typed-address pair branch — SRP-6a over
        the code (``srp.py``), so nothing secret crosses the Wi-Fi — for
        this one pairing: the Pair window's tick, loopback only, echoed
        back as ``allow_typed`` so the window draws the daemon's own answer
        and derives nothing. It is not on the snapshot; the reply's
        ``home_key`` is the QR's and no typed route reads it.
        """
        if not self.lan_access_enabled or not self._api.lan_listening:
            return {"ok": False, "detail": "Phone access is off"}
        hosts, refusal = self._pairing_hosts()
        if refusal or not hosts:
            return {"ok": False, "detail": refusal or
                    "Dark Army could not find an address your phone could reach."}
        code = devices.begin(allow_plain=allow_typed)
        home = devices.pairing_home_key() or b""
        return {
            "ok": True,
            "code": code,
            "allow_typed": bool(allow_typed),
            # `host` is kept beside `hosts` on purpose: a phone build from
            # before this change reads only `host`, and `hosts[0]` is the
            # best candidate, so an old phone scanning a new QR behaves
            # exactly as it did or better.
            "host": hosts[0],
            "hosts": hosts,
            "port": int(self._api._lan_port or LAN_API_PORT),
            "expires_at": time.time() + devices.PAIRING_CODE_SECONDS,
            # The home key, for the QR alone. It crosses loopback to the
            # panel behind `_authorised` — the same boundary the code
            # already crosses — and never the Wi-Fi: a phone that scans the
            # square seals its very first request under it.
            "home_key": base64.b64encode(home).decode("ascii"),
        }

    def pair_bot(self, name: str = "") -> dict:
        """Mint one headless device and hand back its pair reply, once.

        Loopback only — ``_devices`` is the caller, behind the token, the
        Host check and the Origin allowlist. This is not a pairing window:
        the QR stays as it is, and the new row gets its own id, home key,
        relay channel and socket. A phone already paired keeps its channel.

        Refuses unless Away access is on, Socket link is on, and a socket
        address is stored. Those are the same gates a phone's pair reply
        uses before it will carry ``relay_ws_url``; a bot with no socket
        address has nothing to dial. The reply is the phone's pair reply
        plus the home key, because a headless caller has no QR to have
        read the key from. It crosses loopback once. Nothing here writes
        it to a snapshot or a log line.

        A relay mint that fails rolls the device row back, so a refusal
        does not spend one of the eight slots.
        """
        if devices.headless_paired():
            return {"ok": False, "detail": (
                "A bot is already paired. Unpair it in Devices to pair "
                "another.")}
        if not self.remote_access_enabled:
            return {"ok": False, "detail": "Away access is off"}
        if not self.relay_ws_enabled:
            return {"ok": False, "detail": "Socket link is off"}
        if not relay.get_ws_url():
            return {"ok": False,
                    "detail": "Dark Army has no socket address set"}
        token, device_id, label, detail = devices.mint_device(name)
        if not token:
            return {"ok": False,
                    "detail": detail or "could not record the pairing"}
        relay_key, relay_url, relay_ws_url = self._api._mint_relay(device_id)
        if not relay_key or not relay_ws_url:
            devices.unpair(device_id)
            relay.forget(device_id)
            return {"ok": False, "detail": "could not open an away channel"}
        try:
            raw = base64.b64decode(relay_key, validate=True)
            chan = relay.channel_id(raw)
        except (ValueError, TypeError):
            devices.unpair(device_id)
            relay.forget(device_id)
            return {"ok": False, "detail": "could not open an away channel"}
        # A freshly paired bot starts reading with no timer and acting for
        # a day; the person moves either from Devices on the Mac or the
        # phone (`set_bot_access`).
        relay.set_bot_access(device_id, "read", "forever")
        relay.set_bot_access(device_id, "write", "24h")
        home = devices.home_key(device_id) or b""
        return {
            "ok": True,
            "name": label,
            "device_id": device_id,
            "token": token,
            "home_key": base64.b64encode(home).decode("ascii"),
            "relay_key": relay_key,
            "relay_url": relay_url,
            "relay_ws_url": relay_ws_url,
            "channel_id": chan,
        }

    def unpair_device(self, device_id: str) -> tuple:
        """Forget one phone — ledger *and* away channel, so revocation bites
        mid-window: the connector re-reads the key per frame, and a deleted
        key makes the next envelope fail closed. ``(ok, detail)``."""
        ok, detail = devices.unpair(device_id)
        if ok:
            relay.forget(device_id)
        return ok, detail

    def set_bot_access(self, device_id: str, side: str, mode: str, *,
                       requester: str = "") -> tuple:
        """Put one side of the bot's access — ``"read"`` or ``"write"`` —
        in one position. ``(ok, detail)``.

        ``requester`` is the **verified** sender of a sealed frame, or
        ``""`` for the desk; it is never read off a payload. The bot is
        refused by that identity, so it can never lengthen its own grant —
        the whole safety case of the day lease, kept. Any target but the
        headless device is refused, so a phone cannot aim this at a phone's
        away window. Only the desk's own press is filed under "Done
        remotely" here; an away press is filed by the away door's recorder
        and a home press, like every home write, is not filed.
        """
        if devices.is_bot(requester):
            return False, relay.BOT_ACCESS_SELF_REFUSAL
        if not devices.is_bot(device_id):
            return False, relay.BOT_ACCESS_TARGET_REFUSAL
        ok, detail = relay.set_bot_access(device_id, side, mode)
        if ok:
            logger.info("bot %s access set to %s by %s", side, mode,
                        requester or "desk")
            if requester == "":
                self.record_remote_action("desk", "set_bot_access", True)
        return ok, detail

    def devices_snapshot(self) -> dict:
        """What the panel is shown about paired phones. One function, called
        from the API's ``state()``, so the surfaces cannot disagree. **No
        token and no digest ever appears here.** ``lan_enabled`` is whether
        the listener is actually bound, not whether the preference is on —
        a failed bind must draw as off."""
        out = devices.snapshot()
        # Stated, so the panel can tell a daemon that takes sealed home
        # frames from an older one whose devices simply lack the `home`
        # key. Never the key itself.
        out["home_sealed"] = True
        listening = bool(self._api.lan_listening)
        out["lan_enabled"] = bool(self.lan_access_enabled and listening)
        out["port"] = int(self._api._lan_port or LAN_API_PORT)
        # The away path, stated beside the LAN facts. Still no key, digest
        # or token, ever — `relay` per device is a bare "a channel exists",
        # and the lease is a clock reading.
        # Where home is *now*, so a phone whose stored addresses went stale
        # can find its way back onto the LAN instead of living on the relay.
        # Read through the snapshot the phone already polls, which is the one
        # channel an away-at-home phone still has.
        out["hosts"] = self._current_lan_hosts()
        out["remote_enabled"] = bool(self.remote_access_enabled)
        out["relay_url"] = relay.get_url()
        out["remote_activity"] = list(self._remote_activity)
        # Whether the away link is currently working, so "when did away last
        # work?" has an answer without reading the log. Statuses and clock
        # readings only — never the key, its digest, or the channel id the
        # key derives, on a section that rides an ungated `/api/state`.
        if self._relay_connector is not None and self.remote_access_enabled:
            health = self._relay_connector.health_snapshot()
            if health:
                out["relay_health"] = health
        # The socket lane beside it, `relay_health`'s rule exactly: the
        # switch, the address (as `relay_url` already is), and the
        # connector's standing only while it exists and both switches are
        # on. The health record reuses `relay_health`'s five key names, so
        # its two moving fields are already in `_POLL_ECHO_FIELDS`.
        out["relay_ws_enabled"] = bool(self.relay_ws_enabled)
        out["relay_ws_url"] = relay.get_ws_url()
        socket = self._relay_socket
        socket_live = (socket is not None and self.remote_access_enabled
                       and self.relay_ws_enabled)
        if socket_live:
            health = socket.health_snapshot()
            if health:
                out["relay_ws_health"] = health
        last_seen = getattr(self._api, "_device_last_seen", {}) or {}
        for row in out.get("devices") or []:
            did = str(row.get("id") or "")
            row["last_seen"] = float(last_seen.get(did, 0.0) or 0.0)
            row["relay"] = relay.channel_key(did) is not None
            # Presence only, never the token: this section rides an ungated
            # `/api/state`, and an APNs token is an address somebody could
            # spam.
            row["push"] = relay.push_token(did) is not None
            # Same rule for the live card's token: a tick, never the token.
            row["live_activity"] = relay.activity_token(did) is not None
            row["lease_expires_at"] = relay.lease_expires_at(did)
            # A small integer the person at this Mac chose — no key, no
            # digest, no channel id. `0` means they ended away access for
            # this phone; an absent key means an older daemon, which is why
            # the panel's default is -1 rather than 0.
            row["lease_days"] = relay.lease_days(did)
            row["last_frame_at"] = relay.last_frame_at(did)
            # The desk's consent for lock-screen answers, a bare bool.
            row["lock_screen_actions"] = relay.lock_screen_actions(did)
            # The socket lane's standing for this phone, one word out of
            # `relay_ws.SOCKET_WORDS` — never a key, digest, channel id or
            # counter.
            row["socket"] = (socket.socket_word(did) if socket_live
                             else relay_ws.SOCKET_OFF)
            # The bot's two grants, on the bot's row alone: a mode word and
            # a fixed epoch, `relay.bot_grant`'s answer and nothing derived.
            # A phone row carries no such key, and the key's presence is
            # the phone's version marker.
            if devices.is_bot(did):
                # First sight of a bot paired before the grants existed:
                # make its grants explicit and end its day lease, so what
                # is drawn is what is enforced and nothing re-arms it.
                relay.materialise_bot_grants(did)
                access = {}
                for side in relay.BOT_ACCESS_SIDES:
                    mode, until = relay.bot_grant(did, side)
                    access[side] = {"mode": mode, "until": until}
                row["bot_access"] = access
        return out

    def _write_pid(self) -> None:
        """Record this process as the daemon; `_stop_existing_daemon` reads it."""
        ensure_state_dir()
        PID_PATH.write_text(f"{os.getpid()}")

    def _remove_pid(self) -> None:
        PID_PATH.unlink(missing_ok=True)

    #: The loops `run` starts, stopped in this order by `_shutdown`. A name
    #: `run` never reached (history failed to open) is simply not there.
    _BACKGROUND_TASKS = ("_staleness_task", "_liveness_task", "_refresh_task",
                         "_frontmost_task", "_prune_task", "_scan_task")

    #: Stores closed at shutdown, after the decision log, each then cleared.
    _CLOSED_AT_SHUTDOWN = ("_event_log", "_access_log", "_buzz_ledger", "_board")

    @staticmethod
    async def _cancel_and_wait(task) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _shutdown(self, keep_terminals: bool = True) -> None:
        """Stop serving and let go of everything `run` started, in reverse
        dependency order: the relay first, then the background loops, the
        Grok leader, the stores, the terminals, the three listeners, the pid
        file and last the lock."""
        logger.info("Daemon shutting down")
        self._running = False
        self._shutdown_event.set()

        relay_stops = []
        if self._relay_socket is not None:
            relay_stops.append(("relay socket", self._stop_relay_socket))
        if self._relay_connector is not None:
            relay_stops.append(("relay connector", self._relay_connector.stop))
        for what, stop in relay_stops:
            try:
                await stop()
            except Exception:
                logger.warning("%s stop failed", what, exc_info=True)

        for name in self._BACKGROUND_TASKS:
            await self._cancel_and_wait(getattr(self, name, None))
        # A call_later handle rather than a task: cancelling is all it needs.
        armed = self.__dict__.get("_hysteresis_timer")
        if armed is not None:
            armed[0].cancel()
            self._hysteresis_timer = None
        await self._cancel_and_wait(getattr(self, "_leader_watch_task", None))
        # The phone legs in flight, before any store closes: a card buzz
        # sleeping through its grace would otherwise wake to append to a
        # closed ledger or read `_active_notifications` mid-teardown. A
        # cancelled leg writes nothing.
        await self._cancel_phone_legs()

        if self._leader is not None:
            await self._leader.close()
            self._leader = None
            self._grok_leader_up = False
            self._grok_resident = frozenset()

        if self._history is not None:
            self._history.close()
            self._history = None
        if getattr(self, "_decisions", None) is not None:
            await self._decisions.close()
            self._decisions = None
        for name in self._CLOSED_AT_SHUTDOWN:
            store = getattr(self, name, None)
            if store is not None:
                store.close()
                setattr(self, name, None)

        # Quitting or restarting the app leaves the broker, and the agents in
        # its terminals, running; only an explicit close ends a child.
        # `keep_terminals=False` is the old die-with-the-app behaviour.
        try:
            if keep_terminals and self._pty.persist:
                await self._pty.disconnect()
            else:
                await self._pty.close_all()
                await self._pty.stop_broker()
        except Exception:
            logger.debug("closing the daemon's own terminals failed", exc_info=True)
        for listener in (self._socket, self._agents_poller, self._api):
            await listener.stop()
        self._remove_pid()
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def _stop_on_signals(self) -> None:
        """A headless daemon has no app to quit it, so SIGTERM and SIGINT do."""
        def stop():
            asyncio.create_task(self._shutdown())
        for number in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(number, stop)

    def _hand_terminals_the_hook_door(self) -> None:
        """Tell hosted terminals (and a broker spawned from here on) the
        private socket — only when it is bound. A socket nobody serves would,
        under the one address rule, leave a child reporting nowhere; an empty
        value keeps them on the port."""
        self._pty.hook_sock = self._socket.unix_path

    async def run(self) -> None:
        """Take the lock, open every door and loop, and serve until shut down.

        The order matters in two places: the pty broker is attached before
        sessions whose terminal did not come back are retired, and both come
        before the first sweep is scheduled."""
        logging.basicConfig(
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO)
        self._lock = _acquire_lock(takeover=not self._headless)
        self._write_pid()
        self._loop = asyncio.get_running_loop()
        if self._headless:
            self._stop_on_signals()

        await self._socket.start()
        self._hand_terminals_the_hook_door()
        try:
            await self._pty.attach()
        except Exception:
            logger.warning(
                "pty broker unavailable (a greeting over %d bytes, or no "
                "socket); Dark Army will keep trying",
                ptyhost.BROKER_LINE_LIMIT, exc_info=True)
        # Before the first snapshot: a restored session whose terminal did
        # not come back with the broker is dead, and drawing it would say
        # "runs in the editor" about a terminal that never was there.
        self._retire_hostless_sessions()

        self._staleness_task = asyncio.create_task(self._staleness_checker())
        self._liveness_task = asyncio.create_task(self._liveness_checker())
        self._refresh_task = asyncio.create_task(self._snapshot_refresher())
        self._frontmost_task = asyncio.create_task(self._frontmost_checker())
        self._agents_poller.start()

        # History is opened here, not in __init__: constructing a BobDaemon must
        # not create a database as a side effect (the tests do it constantly).
        try:
            self._history = history.HistoryStore()
            self._history.connect()
            self._prune_task = asyncio.create_task(self._history_pruner())
            self._scan_task = asyncio.create_task(self._transcript_scanner())
        except Exception:
            logger.warning("history unavailable; not recording", exc_info=True)
            self._history = None

        from .decision_capture import DecisionCapture
        self._decisions = DecisionCapture()
        await self._decisions.call("open")

        # The diary, on the same terms: a file that will not open is logged
        # and left as None, and every `_log_event` is a no-op on None.
        try:
            diary = event_log.EventLog()
            diary.open()
            self._event_log = diary
            self._seed_logged_starts()
        except Exception:
            logger.warning("event log unavailable; not recording", exc_info=True)
            self._event_log = None

        # The access log, on the diary's terms: a file that will not open is
        # logged and left as None, and `note_access_refusal` records nothing.
        try:
            knocks = access_log.AccessLog()
            knocks.open()
            self._access_log = knocks
        except Exception:
            logger.warning("access log unavailable; not recording", exc_info=True)
            self._access_log = None

        # The buzz ledger, on the same terms: every alert handed to the phone
        # leg leaves one line, and a file that will not open costs nothing
        # but the record.
        try:
            ledger = buzz_ledger.BuzzLedger()
            ledger.open()
            self._buzz_ledger = ledger
        except Exception:
            logger.warning("buzz ledger unavailable; not recording", exc_info=True)
            self._buzz_ledger = None

        # The board, on the same terms and for the same reason. A store that
        # will not open is logged and left as None, and every board verb answers
        # "the board is not open" rather than raising — the rest of Dark Army has
        # nothing to do with cards and must not fail to start over one.
        try:
            self._board = board.BoardStore()
            # Both of these block: `connect` opens SQLite, and the refresh reads
            # the cards *and* the extension lock files. Startup is still the
            # event loop, and "every board call goes through the executor" has
            # no exception for the first one — this is the seam the next caller
            # copies from.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._board.connect)
            await loop.run_in_executor(None, self._sweep_orphan_attachments)
            filled = await loop.run_in_executor(
                None, self._backfill_board_workflows)
            if filled:
                # A bulk permanent write to somebody's store leaves a line
                # saying it happened.
                logger.info("filled expected stages on %d board card(s)",
                            filled)
            await loop.run_in_executor(None, self._refresh_board_state)
            # Announce it once at startup. The reconcile only publishes when
            # something *changed*, so without this the menu bar's ready count
            # would sit at zero until somebody edited a card — a strip that is
            # wrong on every launch until the board is touched.
            self._notify_board()
        except Exception:
            logger.warning("board unavailable; cards will not be shown",
                           exc_info=True)
            self._board = None

        # Grok leader client: constructed here, not in __init__, for the
        # same reason history is — tests construct BobDaemon constantly.
        # connect() only fires when the sock is already there; it will
        # never start a leader.
        self._leader = grok_leader.LeaderClient(
            on_residents=self._on_grok_residents,
            on_down=self._on_grok_leader_down,
        )
        self._leader_watch_task = asyncio.create_task(self._grok_leader_watcher())

        # the panel. A port already in use means another daemon owns it;
        # that is not worth refusing to run over, since everything else still works.
        try:
            await self._api.start()
        except OSError as exc:
            logger.warning("API not started: %s", exc)

        # After the loopback bind, so a collision on the phone port cannot
        # take the panel down with it. The preference was stashed before this
        # loop existed.
        if self.lan_access_enabled:
            await self._api.start_lan()

        # The away connector, applied after the binds for the same reason the
        # LAN listener is: a broken relay setup must cost the away path only.
        if self.remote_access_enabled:
            self._start_relay_connector()
            if self.relay_ws_enabled:
                self._start_relay_socket()

        await self._shutdown_event.wait()
        logger.info("Daemon stopped serving")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Dark Army daemon")
    args = parser.parse_args()

    daemon = BobDaemon()
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
