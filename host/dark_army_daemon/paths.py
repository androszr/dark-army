# host/dark_army_daemon/paths.py
"""Central state-directory paths for Dark Army.

All persistent state lives under ~/.dark-army. Until 22 Sep 2026 the folder was
``~/.bob-companion``, kept as a link (``docs/first-run-checklist.md``, *The
``~/.bob-companion`` link*).

``ensure_state_dir`` is the single seam every real read/write path funnels
through, which makes it the right place to narrow permissions: the directory is
0700 and the files that hold more than "Dark Army is installed" are 0600. They
were not — the directory was world-readable and ``history.db`` inside it was
0644, which put every project path, session title and cost on this machine
within reach of any other account on it.
"""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("dark-army")


def _home() -> Path:
    """Where state lives — the user's home, except under pytest.

    Every constant below is derived from this at import time, so a test that
    forgets to redirect one of them still cannot reach the running fleet's
    files. The panel learned the same lesson the expensive way: its draft store
    defaulted to the real folder, and a plain `swift test` filled the user's
    Drafts sheet with rows a test had typed. Per-file fixtures cover the paths
    somebody remembered; the default is what covers the rest.

    ``pytest`` is only ever importable inside a test run — the frozen py2app
    bundle does not ship it — so production always takes the home branch.
    """
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return Path(tempfile.gettempdir()) / f"dark-army-tests-{os.getpid()}"
    return Path.home()


STATE_DIR = _home() / ".dark-army"

SESSIONS_PATH = STATE_DIR / "sessions.json"
PREFS_PATH = STATE_DIR / "preferences.json"
PID_PATH = STATE_DIR / "daemon.pid"
LOCK_PATH = STATE_DIR / "daemon.lock"
# The PTY broker that outlives a Dark Army restart. The socket is how the
# new daemon reclaims live terminals; the pid file is how a stale socket
# is recognised. Neither holds a transcript — the grid stays in the
# broker's memory.
PTY_SOCK_PATH = STATE_DIR / "pty.sock"
PTY_PID_PATH = STATE_DIR / "pty.pid"
# The hook door (`socket_server.SocketServer`): where the notify script, the
# status-line collector and the channel hand the daemon every hook message.
# A Unix socket rather than a loopback port, so only this account can open it
# — 0600 inside the 0700 state folder, and in `_PRIVATE_FILES` below so the
# re-check narrows it too. `docs/hook-door-contract.md`.
HOOK_SOCK_PATH = STATE_DIR / "hook.sock"
# Note: there is no daemon.log. The daemon logs to stderr, which the menu bar app
# captures into ~/Library/Logs/DarkArmy/dark-army.log.
NOTIFY_SCRIPT_PATH = STATE_DIR / "dark-army-notify"
# The /ship close-out script, installed machine-wide by the menu bar app on
# every launch (hooks.install_close_out_script) so a project's own copy can
# hand off to it and never go stale. 0755 like the notify script, so not in
# _PRIVATE_FILES.
CLOSE_OUT_SCRIPT_PATH = STATE_DIR / "dark-army-close-out"
STATUSLINE_SCRIPT_PATH = STATE_DIR / "dark-army-statusline"
# The shunt guard (hooks.SHUNT_SCRIPT): the PreToolUse hook that refuses a
# whole-file read over the delegation threshold and points the agent at the
# shunt skill instead. A second stdlib-only string installed beside the
# notify script, 0755 like it, so not in _PRIVATE_FILES.
SHUNT_SCRIPT_PATH = STATE_DIR / "dark-army-shunt"
# The channel server's copy (channel_install.CHANNEL_SCRIPT), named here so
# every helper the app installs into the state dir is listed in one place.
CHANNEL_SCRIPT_NAME = "dark-army-channel"
# One empty marker file per session id that the guard must leave alone
# (`.claude/skills/shunt/exempt.py on` writes it, `off` removes it): the
# reviewers' window. A directory, narrowed to 0700 in ensure_state_dir()
# like HOOK_PID_CACHE_DIR and **not** in _PRIVATE_FILES for the same reason.
SHUNT_EXEMPT_DIR = STATE_DIR / "shunt-exempt"
# The delegation ledger: one JSONL file per session id, appended 0600 by the
# shunt wrappers themselves and folded into the card's work record when the
# run ends (`work_record.read_shunt_ledger`). Paths, counts and a cost —
# never file content, a question or a spec. A directory, so 0700 like the
# two above.
SHUNT_LEDGER_DIR = STATE_DIR / "shunt"
# Durable history. Unlike sessions.json (overwritten wholesale with current
# state), this only ever grows — and it outlives the Claude Code transcripts it
# is partly derived from, which are pruned after about a month.
HISTORY_PATH = STATE_DIR / "history.db"
# The Kanban board (board.py). Machine-local by decision — nothing is written
# into a user's repository — which makes this the second SQLite file here, and
# the second one to hold every project path on the machine plus the text of
# every instruction anybody queued.
BOARD_PATH = STATE_DIR / "board.db"
# Per-card copies of files somebody attached while writing a card. The panel
# writes each file 0600 into a staging folder here; the daemon re-checks
# containment at every use. Narrowed as a directory (0700) rather than a
# walk of every copy: files inside a 0700 tree are unreachable by other
# accounts whatever their own mode says.
ATTACHMENTS_DIR = STATE_DIR / "attachments"
# The hook handler's per-session memo of the pid walk's answer (one small JSON
# file per session, written 0600 by the script itself). A directory, so it is
# narrowed to 0700 in ensure_state_dir() like ATTACHMENTS_DIR and is **not** in
# _PRIVATE_FILES — that tuple chmods each name to 0600, which would make a
# directory unusable.
HOOK_PID_CACHE_DIR = STATE_DIR / "hook-pids"
# Names Dark Army minted for sessions Claude Code no longer names (session_title.py).
# Kept out of sessions.json deliberately: that file is overwritten with what is
# *live*, and a title outlives the session's liveness by the width of the
# "Recently finished" section.
TITLES_PATH = STATE_DIR / "titles.json"
# Needs you acknowledgements (inbox_ack.py). Hide-until-changed, not a
# dismiss and not an answer. Next to titles.json so a test daemon writes
# beside its own sessions.json.
INBOX_ACKS_PATH = STATE_DIR / "inbox-acks.json"
# Last Grok / Codex rate-limit figures the chips will stand behind when the
# live source has gone quiet. Percentages of an account, so 0600 with the rest.
USAGE_HOLD_PATH = STATE_DIR / "usage-last.json"
# A finished run's last run-health reading per card (run_health.py): the
# frozen line a card keeps after its session's tombstone has gone, and the
# yardstick the next run in that project is judged against. Card ids, roots
# and counts, so 0600 with titles.json (the name is spelled once, in the
# private ring below).
RUN_HEALTH_PATH = STATE_DIR / 'run-health.json'
# Mission Control's identity (mission.py): the pty broker handle of the one
# standing chief-of-staff terminal, the checkout it runs in and the last
# session id bound to it. A handle and a root are private facts, so 0600 in
# the ring below; read by an older daemon never, by a newer one with a
# default for every key.
MISSION_PATH = STATE_DIR / "mission.json"
# The last successful Rebuild & restart (rebuild_state.py): when it started and
# when it finished, written by the menu-bar app just before it restarts, so the
# fresh daemon can say the rebuild landed. It also holds the last phone press's
# one-time command token (so a replay after the restart is refused), so 0600 in
# `_PRIVATE_FILES` like the other files a token rides.
REBUILD_STAMP_PATH = STATE_DIR / "rebuild-stamp.json"
# Review runs (review_run.py, docs/review-runs.md): one record per run with its
# root and broker handle, so 0600 in the ring below (`review-runs.json`), and a
# folder per run (`review-runs/<id>/`) holding run.json, picks.json and the two
# files the assistant writes (findings.md, steps.md). A directory, 0700 like
# the shunt ledger. Nothing under a project is ever written for a review.
REVIEW_RUNS_PATH = STATE_DIR / "review-runs.json"
REVIEW_RUNS_DIR = STATE_DIR / "review-runs"
# The statusLine command we displaced, if the user already had one. Our script
# shells out to it and prints its output verbatim, so installing Dark Army never costs
# someone the status line they built.
STATUSLINE_CHAIN_PATH = STATE_DIR / "statusline-chain"
# Which projects Dark Army is allowed to watch (enrollment.py). Holds one SHA-256
# digest per enrolled project and never a key, but it also names every enrolled
# project's full path — and it is the authority the gate reads, so it is 0600
# on the same re-check as `board.db`.
ENROLLMENT_PATH = STATE_DIR / "enrollment.json"
# Which projects the user pressed Install agent pack for (pack_ledger.py).
# Names every chosen project path, so 0600 with enrollment.json.
AGENT_PACK_PATH = STATE_DIR / "agent-pack.json"
# The folders an agent may search (search_scope.py): Dark Army's own checkout,
# this state folder and every enrolled project, rewritten on every ledger write
# and once at launch, read by the notify script by exact path. Roots only,
# never a digest or a key, but it names every enrolled project's full path,
# so 0600 with enrollment.json.
SEARCH_SCOPE_PATH = STATE_DIR / "search-scope.json"
# A person's own agent-pack profiles, one folder each holding a
# `profile.json` (pack_render.available_profiles). Read-only to Dark Army:
# nothing writes here, and it names no project, so it is not private.
USER_PROFILES_PATH = STATE_DIR / "profiles"
# The menu-bar app's note to its own replacement (self_restart.py): a schema
# number, up to three unix timestamps of self-restarts inside the last hour,
# and a one-word marker saying the fresh copy still owes the user a notice.
# Deliberately **not** in `_PRIVATE_FILES`: that tuple is the 0600 re-check for
# files naming project paths, instructions, titles or costs, and for files a
# third party (SQLite) creates at 0644. This one names nothing, its only writer
# already passes mode=0o600, and it sits inside a 0700 directory.
RESTART_WATCH_PATH = STATE_DIR / "restart-watch.json"
# Phones paired for LAN read access (devices.py). Holds one SHA-256 digest
# per device and never a token, but it also names every paired phone — 0600
# on the same re-check as `enrollment.json`.
DEVICES_PATH = STATE_DIR / 'devices.json'
# The away-path key store (relay.py). Unlike `devices.json`, this one holds
# the shared secret itself in the clear — the relay key is symmetric and the
# Mac must hold it to seal and open envelopes, so it cannot live in a
# digest-only ledger. 0600 on the same re-check as the rest.
RELAY_PATH = STATE_DIR / 'relay.json'
#: The headless bot's copy of its own pair reply — the phone keeps the
#: same secrets in its Keychain. The daemon never reads it; the bot
#: process does. 0600, beside the other ledgers that hold a key.
BOT_PAIR_NAME = "grok-bot.json"
BOT_PAIR_PATH = STATE_DIR / BOT_PAIR_NAME
#: The three secrets that used to ride in the pair file. The file the bot
#: is given does not carry them; this one stays on the Mac. 0600.
BOT_SECRETS_NAME = "grok-bot.secrets"
BOT_SECRETS_PATH = STATE_DIR / BOT_SECRETS_NAME
#: Bearer for the local connector Grok calls. Not a relay key. 0600.
BOT_MCP_TOKEN_NAME = "grok-bot-mcp-token"
BOT_MCP_TOKEN_PATH = STATE_DIR / BOT_MCP_TOKEN_NAME
# The daemon's diary (event_log.py): one name, so the private-files tuple and
# the path constant below cannot disagree.
EVENT_LOG_NAME = "event-log.jsonl"
# The phone doors' access log (access_log.py): every refused knock on the
# LAN door and the relay mailbox, and every burst alert raised over them.
# Same one-name rule as the diary above.
ACCESS_LOG_NAME = "access-log.jsonl"
# The buzz ledger (buzz_ledger.py): one line per alert the daemon handed to
# the phone leg — session ids, kinds, counts and clock readings, never a word
# of a question, a summary or a banner. Same one-name rule as the diary.
BUZZ_LEDGER_NAME = "buzz-ledger.jsonl"


#: Files here that hold more than the fact that Dark Army is installed, and so are
#: narrowed to this user on every startup. `api-token` — the loopback door's
#: session token; the desk token is never on disk — is created 0600 by
#: `api_server.load_or_create_token` and needs no help; these predate the habit.
#: `history.db` is the one that matters — every project path, session title,
#: model and cost this machine has ever run, and it was mode 0644.
#: `board.db` is here for exactly the reason `history.db` is, and it is the
#: reason the narrowing below is a re-check rather than a latch: SQLite creates
#: a `.db` at 0644 on first connect, long after `ensure_state_dir` first ran, so
#: a once-per-process latch would narrow every file here except the two that
#: matter. It holds every project path and the full text of every instruction
#: anybody queued on the board.
_PRIVATE_FILES = ("decisions.db", "decisions.db-wal", "decisions.db-shm",
                  "history.db", "history.db-shm", "history.db-wal",
                  "board.db",
                  "board.db-shm",
                  "board.db-wal",
                  "sessions.json", "titles.json",
                  "inbox-acks.json",
                  # Dark Army's one importance suggestion per card
                  # (card_priority.py). Card ids and numbers, so it lives in
                  # the 0600 ring beside titles.json.
                  "card-priority.json",
                  # Dark Army's frozen run-health readings (run_health.py): card
                  # ids, project roots and counts, so the same ring.
                  "run-health.json",
                  # The last Rebuild & restart (rebuild_state.py): it holds
                  # the press's one-time command token, so the same ring.
                  "rebuild-stamp.json",
                  # Mission Control's record (mission.py): a broker handle
                  # and a project root, so the same ring.
                  "mission.json",
                  # Review runs' records (review_run.py): project roots and
                  # broker handles, so the same ring.
                  "review-runs.json",
                  # Written by the panel, never read by the daemon: half-typed
                  # cards, which is project paths and instructions nobody has
                  # submitted. The panel chmods it 0600 on every write; this
                  # sweep is the belt to that braces, for a file left behind by
                  # an interrupted or older panel.
                  "card-drafts.json",
                  "preferences.json",
                  "enrollment.json",
                  AGENT_PACK_PATH.name,
                  SEARCH_SCOPE_PATH.name,
                  "devices.json",
                  "relay.json",
                  # The bot's pair reply (relay_bot.py). Keys, so the same
                  # ring as relay.json. The daemon does not read it.
                  BOT_PAIR_NAME,
                  BOT_SECRETS_NAME,
                  BOT_MCP_TOKEN_NAME,
                  "identities.json", "statusline-chain",
                  "usage-last.json",
                  # The diary the phone reads: every project name, session
                  # title and permission ask of the last day.
                  EVENT_LOG_NAME,
                  # And the access log: the address of every stranger who
                  # knocked, and which paired phone's mailbox a bad envelope
                  # landed in. Never a key, a channel id or a pairing code.
                  ACCESS_LOG_NAME,
                  # And the buzz ledger: which session was buzzed about, when
                  # and why — session ids, kinds, counts and clocks, no words.
                  BUZZ_LEDGER_NAME,
                  "pty.pid",
                  # The hook door: bound 0600 already, narrowed again here
                  # in case a bind ever raced a looser umask.
                  HOOK_SOCK_PATH.name)

#: The daemon's bounded diary of what happened (`event_log.py`), read by the
#: phone's Recently section. One JSON line per event, ≤ 500 lines, ≤ 24 h.
EVENT_LOG_PATH = STATE_DIR / EVENT_LOG_NAME
#: The phone doors' access log (`access_log.py`): one JSON line per refused
#: knock or burst alert, ≤ 2000 lines, ≤ 30 days.
ACCESS_LOG_PATH = STATE_DIR / ACCESS_LOG_NAME
#: The buzz ledger (`buzz_ledger.py`): one JSON line per alert handed to the
#: phone leg, ≤ 2000 lines, ≤ 30 days.
BUZZ_LEDGER_PATH = STATE_DIR / BUZZ_LEDGER_NAME


#: When the narrowing below last ran, and how often it may run again. Not a
#: once-per-process latch, which is what this was first: `history.db` is created
#: by SQLite on first connect — 0644, and long after the first
#: ``ensure_state_dir`` — so a latch would narrow every file except the one that
#: matters most. Re-checking is what heals that, and the cost is a handful of
#: ``stat`` calls.
#:
#: **This is not a timer, and the interval is a floor rather than a period.**
#: Nothing schedules ``ensure_state_dir``: it runs when something else touches
#: the state directory, so the narrowing happens on the next such touch that is
#: at least ``_RESTRICT_INTERVAL`` after the last one. Measured on a live
#: daemon, ``board.db`` stayed 0644 for over two minutes after SQLite created
#: it. The exposure window is bounded by how busy the fleet is, not by the
#: clock, and that is accepted — it is the same for ``history.db``, and a timer
#: to shorten it would be a thread whose whole job is a ``chmod`` that has
#: almost always already happened.
_RESTRICT_INTERVAL = 60.0
_restricted_at = 0.0


def atomic_write_json(path, obj, mode: int = 0o600, indent=None) -> None:
    """Write `obj` as JSON at `path`, durably and atomically.

    One shape for every JSON writer under the state dir: a `mkstemp` sibling in
    the target's own directory (so `os.replace` is a same-filesystem rename),
    narrowed to `mode` before a byte is written, flushed **and fsynced** before
    the replace — without the fsync a crash between the rename and the kernel's
    own writeback can leave a zero-length or truncated file wearing the real
    name, which is exactly the corruption an "atomic" writer exists to prevent.

    Raises `OSError` on failure — how loud to be is the caller's decision — and
    the temp file never survives, success or not.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restrict(path: Path, mode: int) -> None:
    """Narrow `path` to this user. Never raises: a permission we could not set
    is worth a log line, not a daemon that will not start."""
    try:
        if path.exists() and (path.stat().st_mode & 0o777) != mode:
            path.chmod(mode)
    except OSError:
        logger.warning("Could not restrict permissions on %s", path)


def ensure_state_dir() -> Path:
    """Create the state dir and keep it private.

    Idempotent and cheap: between permission sweeps it only does the
    ``mkdir(exist_ok=True)``. Safe to call from any process, on every state
    read/write. Returns ``STATE_DIR``.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    global _restricted_at
    now = time.monotonic()
    if now - _restricted_at >= _RESTRICT_INTERVAL:
        # Not on every call: this function is on every state read and write and
        # its docstring promises to be cheap.
        _restricted_at = now
        _restrict(STATE_DIR, 0o700)
        for name in _PRIVATE_FILES:
            _restrict(STATE_DIR / name, 0o600)
        # Directory only, and only when it exists: the panel creates this on
        # the first attach, and a chmod of a missing path is a wasted stat.
        if ATTACHMENTS_DIR.exists():
            _restrict(ATTACHMENTS_DIR, 0o700)
        if HOOK_PID_CACHE_DIR.exists():
            _restrict(HOOK_PID_CACHE_DIR, 0o700)
        # The shunt guard's exemption markers and the delegation ledgers are
        # written by scripts running inside an agent's session, so the
        # daemon narrows the directories on the same re-check rather than
        # trusting every writer to have passed the mode.
        if SHUNT_EXEMPT_DIR.exists():
            _restrict(SHUNT_EXEMPT_DIR, 0o700)
        if SHUNT_LEDGER_DIR.exists():
            _restrict(SHUNT_LEDGER_DIR, 0o700)
        if REVIEW_RUNS_DIR.exists():
            _restrict(REVIEW_RUNS_DIR, 0o700)
    return STATE_DIR
