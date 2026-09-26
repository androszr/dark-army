"""Dark Army's footprint in each assistant's own configuration.

The hook handler every session runs (`NOTIFY_SCRIPT`, written out as a file),
the groups that name it in Claude Code's settings, Grok's hooks file and
Codex's, the shunt guard beside it, and the few settings keys Dark Army owns.
Every install merges: a person's own hooks are never removed or reordered.
"""

import copy
import json
import logging
import os
import re
import shutil
import tempfile
import time
import stat
import textwrap
import tomllib
from pathlib import Path
from typing import Optional

from dark_army_daemon.protocol import POST_TOOL_USE_MATCHER
from dark_army_daemon.paths import (CLOSE_OUT_SCRIPT_PATH,
                                        NOTIFY_SCRIPT_PATH, SHUNT_SCRIPT_PATH,
                                        ensure_state_dir)
from dark_army_daemon.subprocess_env import PY2APP_ENV_VARS

logger = logging.getLogger("dark-army.hooks")

CLAUDE_SETTINGS_PATH = Path.home().joinpath(".claude", "settings.json")
GROK_HOOKS_DIR = Path.home() / ".grok" / "hooks"
GROK_HOOKS_PATH = GROK_HOOKS_DIR / "dark-army.json"
GROK_CONFIG_PATH = Path.home() / ".grok" / "config.toml"
GROK_RULES_DIR = Path.home() / ".grok" / "rules"
GROK_RULES_PATH = GROK_RULES_DIR / "dark-army.md"

# Grok ignores SessionStart stdout, so the TL;DR / bob-actions hint Claude
# gets from the notify script never reaches it. A home-level rule is the
# channel Grok actually loads at session start. Owned file: we replace only
# this name, never anyone else's in the same folder.
GROK_RULES_TEXT = (
    "# Dark Army\n"
    "\n"
    "When you stop to wait on the user — a question, a plan to approve, a "
    "choice — prefer the `ask_user_question` tool so the answers become "
    "buttons. If that tool is not available, still name the concrete "
    "answers on their own line:\n"
    "\n"
    "<!-- bob-actions: Accept | Iterate -->\n"
    "\n"
    "Up to three labels, each the short word you want typed back, and only "
    "when they are genuinely the answers. Also end that waiting message "
    "with a one-sentence summary:\n"
    "\n"
    "<!-- bob-tldr: your one short sentence -->\n"
    "\n"
    "A finished piece of work that is not waiting on anyone lists neither "
    "marker.\n"
    "\n"
    "When you finish a piece of work the user asked for — the task is "
    "complete and you are not waiting on anything — end that final "
    "message with a standard completion report. Its shape is fixed: a "
    "heading line reading exactly ## Work done, then four labelled lines "
    "in this order — **Asked:** (the request, one or two sentences, in "
    "the user's terms), **Changed:** (what changed, up to about six "
    "short bullets or sentences; file paths are welcome), **Verified:** "
    "(each check you ran and its result, in plain words), **Unchecked:** "
    "(what nobody verified, written as numbered imperative steps — one "
    "action per line, naming the surface to open, the thing to press and "
    "what they should see — then one line beginning 'Why not automated:' "
    "— or exactly the words: Nothing - every check above ran.). A section "
    "with nothing to say states that out loud rather than disappearing. "
    "Keep the whole report under 25 lines. This report ends the work: it "
    "is not a question, so list neither marker on that message.\n"
    "\n"
    "## Where to search\n"
    "\n"
    "Never run a recursive file walk - find, grep -r or -R, rg, fd, ls -R, "
    "du, tree, mdfind without -onlyin, or anything like them - rooted at /, "
    "at your home folder, or at Documents, Desktop, Downloads, Pictures, "
    "Music, Movies or Library inside it. On this Mac such a walk is charged "
    "to Dark Army and raises Photos, Music and Documents privacy prompts "
    "naming it. Search only inside this project, Dark Army's own folder "
    "~/.dark-army, this session's own scratch folder, and the folders Dark "
    "Army knows (its checkout and every project onboarded to it, either "
    "direction): they are listed in ~/.dark-army/search-scope.json - read "
    "that one file (it holds folder paths and nothing else); never search "
    "for it. Reading a file at a known exact path is always fine. A plan or "
    "card file is never something to search for: a session started from a "
    "Dark Army card was handed its plan path, a card's plan_path is on the "
    "board (the dark_army_* board tools or Dark Army's panel), and "
    "otherwise ask the person for the path.\n"
)
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
# Codex's user-layer hooks file: the same `{"hooks": {"<Event>": [...]}}`
# JSON as Claude's settings block (codex-cli 0.155.1 loads it beside
# `config.toml` and warns when both carry hooks). Only the shunt guard is
# written here — the notify script has nothing to say to Codex, which is
# observed through its journals — and the person trusts hooks once in the
# TUI before Codex runs any of it (`docs/codex-ship.md`, step 7).
CODEX_HOOKS_PATH = Path.home() / ".codex" / "hooks.json"

# The hook handler, as the text of the file it is installed as. It runs under
# whatever `python3` the session has (3.9 included), from a folder with none
# of our packages in it: the standard library only, and no newer syntax.
NOTIFY_SCRIPT = textwrap.dedent('''\
    #!/usr/bin/env python3
    """dark-army-notify - hook handler for Dark Army.

    Reads a Claude Code or Grok hook payload from stdin, converts it to a
    daemon message, and forwards it through Dark Army's private hook socket.
    No external dependencies.
    """
    # NOTIFY_SCRIPT_VERSION: 2026-09-23-private-hook-socket

    import json
    import os
    import re
    import socket
    import sys
    import time

    # Deferred: every event pays interpreter start-up, so the modules only
    # some events need are imported at the moment they are first needed.
    # `subprocess` is rebound as a module global inside _process_table() so a
    # test may still stand in for it by name; `pathlib` and `urllib.parse`
    # live inside _grok_session_cwd() (Grok sessions only) and `uuid` inside
    # _run_permission_broker() (approval prompts only).
    subprocess = None

    # The one address rule, the same in every Dark Army client: a socket path
    # named in the environment is the only address; else a port named there
    # (the bridge for sessions from before the socket, on its way out); else
    # the private socket in Dark Army's own folder. Explicit means exclusive:
    # there is never a second try at another door, or a helper pointed at a
    # dead address on purpose would reach the daemon after all.
    HOOK_SOCKET = os.environ.get("DARK_ARMY_HOOK_SOCKET", "")
    HOOK_PORT = os.environ.get("BOB_COMPANION_PORT") or os.environ.get("CLAWD_TANK_PORT", "")
    DEFAULT_HOOK_SOCKET = os.path.join(os.path.expanduser("~"), ".dark-army", "hook.sock")
    # The folders an agent may search, written by Dark Army on every enrolment
    # change and at launch (search_scope.py). Read by this exact path on
    # session_start only; never found by walking.
    SEARCH_SCOPE_PATH = os.path.join(os.path.expanduser("~"), ".dark-army", "search-scope.json")


    def _hook_connect(timeout):
        """A socket connected to Dark Army's hook door. Raises OSError when
        nothing answers, ValueError for a port that is not a number."""
        if HOOK_SOCKET or not HOOK_PORT:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(HOOK_SOCKET or DEFAULT_HOOK_SOCKET)
            except OSError:
                sock.close()
                raise
            return sock
        return socket.create_connection(("127.0.0.1", int(HOOK_PORT)), timeout=timeout)

    # One standing instruction, injected at session start: Claude Code adds a
    # SessionStart hook's stdout to the model's context, and Dark Army already runs
    # this script in every session it can monitor — which is the whole
    # coverage argument. A line in one repo's CLAUDE.md reaches one project; a
    # line in ~/.claude/CLAUDE.md reaches everything but only by editing a
    # file the user owns; this reaches exactly the sessions whose rows exist,
    # and turning the preference off stops it at the next session with nothing
    # to un-edit. The marker it asks for is the one session_stats.py parses
    # (`_TLDR_RE`) — a test holds the two ends of that contract together.
    TLDR_HINT = (
        "A local session monitor (Dark Army) shows a one-line status for this "
        "session. Whenever you stop to wait on the user - a question, a plan "
        "to approve, a choice - end that message with a one-sentence "
        "plain-language summary of what you need from them, wrapped in an "
        "HTML comment on its own line: <!-- bob-tldr: your one short "
        "sentence -->. Plain words a non-developer could read, no paths or "
        "identifiers. The comment is invisible in rendered output. If that "
        "message offers the user a choice between concrete answers - accept "
        "or iterate, one approach or another - also list those answers on "
        "their own line: <!-- bob-actions: Accept | Iterate -->. Up to three, "
        "each the word you want typed back, and only when they are genuinely "
        "the answers; a message with nothing to decide lists none."
    )

    # The second standing instruction, same vehicle and same coverage argument
    # as TLDR_HINT above, pointing the opposite way: the TL;DR marks a turn
    # that is *waiting on somebody*, this marks a turn that is waiting on
    # nobody — finished work must end with a report the person can read in the
    # terminal, not a wiped screen. The shape is dictated verbatim so two
    # sessions in two projects produce recognisably the same thing; the labels
    # are pinned by a test. The report is for the person in the terminal; the
    # daemon slices it on the heading (`session_stats._work_report`, into
    # `last_report`) and structures it for drawing (`work_report.py`), and
    # infers no state from it. This text must
    # never contain a
    # literal bob-actions or bob-tldr HTML comment (a contract test splits
    # stdout on the first such marker).
    WORK_REPORT_HINT = (
        "When you finish a piece of work the user asked for - the task is "
        "complete and you are not waiting on anything - end that final "
        "message with a standard completion report. Its shape is fixed: a "
        "heading line reading exactly ## Work done, then four labelled lines "
        "in this order - **Asked:** (the request, one or two sentences, in "
        "the user's terms), **Changed:** (what changed, up to about six "
        "short bullets or sentences; file paths are welcome), **Verified:** "
        "(each check you ran and its result, in plain words), **Unchecked:** "
        "(what nobody verified, written as steps - see below - or "
        "exactly the words: Nothing - every check above ran.). A section with nothing to say states that out loud rather "
        "than disappearing. "
        "The Unchecked section is written for somebody who will do the "
        "checking, so it is numbered imperative steps and never a hint: one "
        "action per line, naming the surface to open, the thing to press and "
        "what they should see, in words a non-developer could follow and "
        "with no identifiers they cannot act on. After the steps, one line "
        "beginning 'Why not automated:' saying why a test could not make "
        "that observation - it is required, and a check that cannot say why "
        "it is manual is one that should have been a test. So prefer the "
        "test: leave something here only when the observation genuinely "
        "cannot be made in code - a real window on a real screen, a second "
        "real terminal, a timing you can only feel - and if you are handing "
        "back more than two of these, look again for the seam that would "
        "automate them. "
        "If this session is working a Dark Army board card and a tool named "
        "dark_army_close_card (or bob_close_card, in a session started "
        "before the rename) is available, finishing the work means calling "
        "it - "
        "the close is the required last act of the work, not an optional "
        "extra, whether or not a check is left for a person. Give it a note "
        "of one or two sentences distilled from "
        "the report's Asked and Verified lines; the note is clamped at 400 "
        "characters, so never paste the report itself. Then add one more "
        "line, **Card:**, saying you moved it to Done. When a check is left "
        "outstanding, first write it as its own file at "
        "manual-check/<YYYY-MM-DD>-<slug>/check.md in the project - the "
        "answer block, the numbered steps and the reason - and run python3 "
        ".claude/skills/ship/manual_check.py on it where the project has "
        "that checker; when a tool for flagging your card is available, "
        "call it with those same numbered steps and the file's path so the "
        "board shows the check is waiting on a person, then call the close "
        "tool; the **Card:** line names the check file. No card or no such "
        "tool means no Card line. "
        "Keep the whole report under 25 lines. This report ends the "
        "work: it is not a question, so put no bob-tldr and no bob-actions "
        "comment on that message."
    )

    # The third standing instruction, same vehicle: where an agent may search.
    # A recursive walk from / or the home folder in a terminal Dark Army hosts
    # is charged to Dark Army by macOS and raises Photos, Music and Documents
    # prompts naming it (24 Sep 2026). The {roots} slot is filled from the
    # search-scope file at the moment the session starts, one folder a line,
    # or with the fallback words when the file is missing or malformed. Like
    # WORK_REPORT_HINT it must never contain a literal bob-actions or
    # bob-tldr HTML comment.
    SEARCH_SCOPE_HINT = (
        "Where you may search: never run a recursive file walk - find, grep "
        "-r or -R, rg, fd, ls -R, du, tree, mdfind without -onlyin, or "
        "anything like them - rooted at /, at your home folder, or at "
        "Documents, Desktop, Downloads, Pictures, Music, Movies or Library "
        "inside it. On this Mac such a walk is charged to Dark Army and "
        "raises Photos, Music and Documents privacy prompts naming it. "
        "Search only inside this project, Dark Army's own folder "
        "~/.dark-army, this session's own scratch folder, and these folders "
        "Dark Army knows (its checkout and every project onboarded to it, "
        "either direction):\\n{roots}\\n"
        "Reading a file at a known exact path is always fine. A plan or card "
        "file is never something to search for: a session started from a "
        "Dark Army card was handed its plan path, a card's plan_path is on "
        "the board (the dark_army_* board tools or Dark Army's panel), and "
        "otherwise ask the person for the path."
    )
    SEARCH_SCOPE_FALLBACK = (
        "(Dark Army has not written its folder list yet - search only this "
        "project, ~/.dark-army and your scratch folder)"
    )


    def _search_scope_hint():
        """SEARCH_SCOPE_HINT with the folders from the search-scope file, one
        a line; the fallback words on any missing, unreadable or malformed
        file. Never raises."""
        roots = None
        try:
            with open(SEARCH_SCOPE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            found = data.get("roots") if isinstance(data, dict) else None
            if (isinstance(found, list) and found
                    and all(isinstance(r, str) and r.strip() and "\\n" not in r
                            for r in found)):
                roots = found
        except (OSError, ValueError):
            roots = None
        if roots:
            listed = "\\n".join("  - " + r for r in roots)
        else:
            listed = SEARCH_SCOPE_FALLBACK
        return SEARCH_SCOPE_HINT.format(roots=listed)

    # The broker's patience, and the whole of its safety case. Every wait is
    # bounded: 3s per socket operation, a poll every BROKER_POLL_SECONDS, and
    # a hard BROKER_WAIT_SECONDS deadline from branch entry, under the
    # timeout pinned on the hook entry itself so the script always exits on
    # its own terms rather than being killed mid-write.
    #
    # **Why the deadline is half an hour.** The whole of it rests on the
    # terminal's own dialog running *concurrently* with this hook, so that
    # either answer wins and holding here costs the person at the desk
    # nothing. The CLI also has an `awaitAutomatedChecksBeforeDialog` mode
    # (set in its `requestDialog` branch, and in the async-subagent spawn
    # context) that awaits the hooks *before* drawing the dialog — and there
    # a hold is a freeze with nothing on screen to answer. The subagent case
    # is refused daemon-side; for the ordinary path the concurrency was
    # unproven, so this used to be set to what was survivable if it were
    # false.
    #
    # It is proven now, on Claude Code **2.1.263**, and the evidence is
    # `docs/2026-09-07-permission-hold-verification.md`: the dialog was drawn
    # on the session's own terminal while this script was holding, moved its
    # selection under a cursor key mid-hold, and each answer won when it was
    # given first — Dark Army's, credited by the CLI as "Allowed by
    # PermissionRequest hook", and the keyboard's, after which no hook credit
    # appeared and a late verdict from Dark Army was refused.
    #
    # That property belongs to the installed CLI, not to Dark Army, so **a CLI bump
    # re-runs `tools/permission_hold_livefire.py`** and this figure follows
    # its VERDICT line. The record covers the ordinary PermissionRequest path
    # on that version only; an ask raised inside a subagent is not covered
    # and is refused daemon-side.
    BROKER_POLL_SECONDS = 2.0
    BROKER_WAIT_SECONDS = 1800.0
    BROKER_SOCKET_TIMEOUT = 3.0
    MAX_BROKER_DESCRIPTION_CHARS = 300
    MAX_BROKER_INPUT_CHARS = 400

    _CLAUDE_ARGV_RE = re.compile(r"(^|/)claude($|\\s)")
    _GROK_ARGV_RE = re.compile(r"(^|/)grok($|\\s)")
    # Shared `grok agent leader` — not a session. Keep in sync with
    # pid_resolver.is_grok_leader. `grok agent --leader` must not match.
    _GROK_LEADER_RE = re.compile(r"(?:^|/)grok(?:\\s|$).*\\bagent\\s+leader\\b")
    _STOP_TEARDOWN = frozenset(("channel_closed", "shutdown"))
    _EVENT_ALIASES = {
        "session_start": "SessionStart",
        "pre_tool_use": "PreToolUse",
        "post_tool_use": "PostToolUse",
        "permission_request": "PermissionRequest",
        "post_tool_use_failure": "PostToolUseFailure",
        "pre_compact": "PreCompact",
        "stop": "Stop",
        "stop_failure": "StopFailure",
        "stop_cancelled": "StopCancelled",
        "notification": "Notification",
        "user_prompt_submit": "UserPromptSubmit",
        "session_end": "SessionEnd",
        "subagent_start": "SubagentStart",
        "subagent_stop": "SubagentStop",
        "subagent_end": "SubagentStop",
    }


    def _process_table():
        """One `ps` per hook invocation: pid -> (ppid, command).

        The walk used to run up to three `ps -o <field>= -p <pid>` subprocesses
        per ancestor; on every hook event that added up. One `-axo` listing
        parsed once answers every rung. `command` is the full argv, which the
        regexes above match against; a separate `comm` read added nothing they
        do not already cover — a native `claude`/`grok` binary's argv[0] is a
        path the same patterns match.
        """
        global subprocess
        if subprocess is None:
            import subprocess
        try:
            r = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,command="],
                capture_output=True, text=True, timeout=2.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            return {}
        table = {}
        for line in r.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            try:
                entry_pid = int(parts[0])
                entry_ppid = int(parts[1])
            except ValueError:
                continue
            table[entry_pid] = (entry_ppid, parts[2] if len(parts) > 2 else "")
        return table


    def _looks_claude(comm, command):
        return comm == "claude" or bool(_CLAUDE_ARGV_RE.search(command))


    def _looks_grok(comm, command):
        if command and _GROK_LEADER_RE.search(command):
            return False
        return comm == "grok" or bool(_GROK_ARGV_RE.search(command))


    def _walk_session_pid():
        """Climb the parent chain from this hook to the Claude Code or Grok
        process that owns the session; `pid_resolver` holds the same walk.

        Returns ``(pid, provider, found)``: `found` is True on every return
        naming a matched claude/grok ancestor and False on the final fallback
        to the start pid, which names the hook's own short-lived parent and
        must never be remembered.
        """
        prefer_grok = bool(os.environ.get("GROK_SESSION_ID"))
        table = _process_table()
        start = os.getppid()
        pid = start
        found_claude = None
        found_grok = None
        skip_grok_ancestors = False
        seen = set()
        while pid > 1 and pid in table and pid not in seen:
            seen.add(pid)
            ppid, command = table[pid]
            if _looks_claude("", command):
                found_claude = pid
            elif command and _GROK_LEADER_RE.search(command):
                # Shared leader. Its parent is some other tab's TUI —
                # walking further stamps that pid on every session.
                # Keep in sync with pid_resolver.find_session_pid.
                if prefer_grok:
                    break
                skip_grok_ancestors = True
            elif _looks_grok("", command) and not skip_grok_ancestors:
                found_grok = pid
            if prefer_grok and found_grok is not None:
                return found_grok, "grok", True
            if not prefer_grok and found_claude is not None:
                return found_claude, "claude", True
            pid = ppid
        if prefer_grok and found_grok is not None:
            return found_grok, "grok", True
        if found_claude is not None:
            return found_claude, "claude", True
        if found_grok is not None:
            return found_grok, "grok", True
        return start, ("grok" if prefer_grok else "claude"), False


    def _find_session_pid():
        """The walk's two-tuple answer, uncached."""
        pid, provider, _found = _walk_session_pid()
        return pid, provider


    # ── the pid memo ─────────────────────────────────────────────────────
    #
    # The walk above costs one `ps -axo` fork — two thirds of every hook
    # invocation — and answers the same question hundreds of times per
    # session. The answer is remembered per session under Dark Army's private
    # state directory and believed only while the named pid provably still
    # is the process it was: its kernel start time (to the microsecond) and
    # short name are re-read on every hit, so a recycled pid — even one
    # recycled by another `claude` — is a miss. A fallback answer (the walk
    # found nothing and named the hook's own parent) is never written.
    # Every failure here — no library, a full disk, a bad permission —
    # returns to the walk; nothing raises.
    PID_CACHE_TTL_SECONDS = 86400
    MAX_PID_CACHE_ENTRIES = 200
    MAX_CHAIN_RUNGS = 32
    _PROC_PIDTBSDINFO = 3
    _proc_identity_lib = None


    def _proc_bsdinfo(pid):
        """``(start_tvsec, start_tvusec, comm, ppid)`` for `pid`, or None.

        A capability probe, not a platform test: where the library or the
        symbol is absent, or the answer is not exactly the struct's size, the
        result is None and the memo is inert.
        """
        global _proc_identity_lib
        try:
            pid = int(pid)
            if pid <= 0:
                return None
            import ctypes

            class _BSDInfo(ctypes.Structure):
                # struct proc_bsdinfo, declared up to pbi_start_tvusec; the
                # kernel's own return value is checked against sizeof().
                _fields_ = [
                    ("pbi_flags", ctypes.c_uint32),
                    ("pbi_status", ctypes.c_uint32),
                    ("pbi_xstatus", ctypes.c_uint32),
                    ("pbi_pid", ctypes.c_uint32),
                    ("pbi_ppid", ctypes.c_uint32),
                    ("pbi_uid", ctypes.c_uint32),
                    ("pbi_gid", ctypes.c_uint32),
                    ("pbi_ruid", ctypes.c_uint32),
                    ("pbi_rgid", ctypes.c_uint32),
                    ("pbi_svuid", ctypes.c_uint32),
                    ("pbi_svgid", ctypes.c_uint32),
                    ("rfu_1", ctypes.c_uint32),
                    ("pbi_comm", ctypes.c_char * 16),
                    ("pbi_name", ctypes.c_char * 32),
                    ("pbi_nfiles", ctypes.c_uint32),
                    ("pbi_pgid", ctypes.c_uint32),
                    ("pbi_pjobc", ctypes.c_uint32),
                    ("e_tdev", ctypes.c_uint32),
                    ("e_tpgid", ctypes.c_uint32),
                    ("pbi_nice", ctypes.c_int32),
                    ("pbi_start_tvsec", ctypes.c_uint64),
                    ("pbi_start_tvusec", ctypes.c_uint64),
                ]

            if _proc_identity_lib is None:
                lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
                fn = lib.proc_pidinfo
                fn.restype = ctypes.c_int
                fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                               ctypes.c_void_p, ctypes.c_int]
                _proc_identity_lib = fn
            info = _BSDInfo()
            size = ctypes.sizeof(info)
            got = _proc_identity_lib(pid, _PROC_PIDTBSDINFO, 0,
                                     ctypes.byref(info), size)
            if got != size:
                return None
            comm = info.pbi_comm.decode("utf-8", "replace")
            return (int(info.pbi_start_tvsec), int(info.pbi_start_tvusec), comm,
                    int(info.pbi_ppid))
        except Exception:
            return None


    def _proc_identity(pid):
        """``(start_tvsec, start_tvusec, comm)`` for `pid`, or None."""
        info = _proc_bsdinfo(pid)
        return None if info is None else info[:3]


    def _memo_on_our_chain(pid):
        """True only where `pid` is an ancestor of this hook.

        Identity proves the remembered pid is still the same process; it does
        not prove *this* hook descends from it. Two harnesses can share one
        session id (a `--resume` of a session another terminal `/clear`ed
        away from), and the memo written by the first must not answer for the
        second — that would aim Stop at the wrong terminal. Read off the
        kernel's own parent links through the same probe, never a fork; any
        doubt is False, which is a miss.
        """
        try:
            cur = os.getppid()
            for _ in range(MAX_CHAIN_RUNGS):
                if cur == pid:
                    return True
                if cur <= 1:
                    return False
                info = _proc_bsdinfo(cur)
                if info is None:
                    return False
                cur = info[3]
        except Exception:
            pass
        return False


    def _cache_dir():
        # Recomputed on every call so a test needs only HOME.
        return os.path.join(os.path.expanduser("~"), ".dark-army", "hook-pids")


    def _cache_key(session_id, prefer_grok):
        sid = str(session_id or "")
        if not sid:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:64]
        return ("g-" if prefer_grok else "c-") + safe


    def _cache_path(key):
        return os.path.join(_cache_dir(), key + ".json")


    def _cache_read(key):
        try:
            with open(_cache_path(key), "r", encoding="utf-8") as fh:
                entry = json.loads(fh.read(4096))
            return entry if isinstance(entry, dict) else None
        except Exception:
            return None


    def _cache_write(key, entry):
        path = _cache_path(key)
        tmp = path + "." + str(os.getpid()) + ".tmp"
        try:
            os.makedirs(_cache_dir(), 0o700, exist_ok=True)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, json.dumps(entry, separators=(",", ":")).encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass


    def _prune_cache():
        """Once per write: drop entries older than a day, then the oldest
        beyond the ceiling. Best effort."""
        try:
            now = time.time()
            kept = []
            with os.scandir(_cache_dir()) as it:
                for de in it:
                    try:
                        if not de.is_file():
                            continue
                        mtime = de.stat().st_mtime
                    except OSError:
                        continue
                    if now - mtime > PID_CACHE_TTL_SECONDS:
                        try:
                            os.unlink(de.path)
                        except OSError:
                            pass
                        continue
                    kept.append((mtime, de.path))
            if len(kept) > MAX_PID_CACHE_ENTRIES:
                kept.sort()
                for _mtime, path in kept[:len(kept) - MAX_PID_CACHE_ENTRIES]:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except Exception:
            pass


    def _session_pid_cached(session_id, prefer_grok):
        """The walk's answer for this session, remembered across events.

        A hit forks nothing: the remembered pid must still carry the recorded
        identity **and** sit on this hook's own ancestor chain. A miss walks,
        and remembers the answer only when the walk positively identified an
        ancestor and that pid's identity could be read.
        """
        key = _cache_key(session_id, prefer_grok)
        if key is None:
            return _walk_session_pid()[:2]
        entry = _cache_read(key)
        if entry is not None:
            try:
                pid = entry.get("pid")
                provider = entry.get("provider")
                ok = (
                    entry.get("v") == 1
                    and isinstance(pid, int) and not isinstance(pid, bool) and pid > 1
                    and provider in ("claude", "grok")
                    and isinstance(entry.get("at"), (int, float))
                    and time.time() - entry["at"] <= PID_CACHE_TTL_SECONDS
                )
                if ok:
                    ident = _proc_identity(pid)
                    if (ident is not None and list(ident) == entry.get("id")
                            and _memo_on_our_chain(pid)):
                        return pid, provider
            except Exception:
                pass
        pid, provider, found = _walk_session_pid()
        if found:
            ident = _proc_identity(pid)
            if ident is not None:
                _cache_write(key, {"v": 1, "pid": pid, "provider": provider,
                                   "id": list(ident), "at": time.time()})
                _prune_cache()
        return pid, provider


    # ── the project's enrolment key ───────────────────────────────────────
    #
    # Dark Army only watches projects you enrolled. Enrolment puts a private key file
    # inside the project; every message this script sends carries it, and the
    # daemon turns away anything that arrives without one. It rides on *every*
    # message rather than once per session: this script is fire-and-forget and
    # one-shot per event, there is no process to hold an admission ticket, and
    # un-enrolling a project has to bite on the next thing it says rather than
    # at the end of a session that may run for hours.
    #
    # **Two folder names, the new one first.** The key lives in
    # `<root>/.dark-army/key`; a project enrolled before the move may still
    # hold only `<root>/.bob-companion/key`, which is read second for as long
    # as the read window is open (Dark Army copies the key across on launch).
    #
    # **The walk-up must skip the home directory, under both names.**
    # `~/.dark-army` is Dark Army's own state folder and `~/.bob-companion`
    # still exists on an upgraded machine as a link to it — both are the names
    # a project's key folder has, so without that skip a session anywhere
    # under $HOME would find a `key` there and enrol the whole home directory
    # by accident. The skip is per *directory*, so it covers every name.
    KEY_DIR_NAMES = (".dark-army", ".bob-companion")
    KEY_FILE_NAME = "key"
    MAX_KEY_BYTES = 512
    MAX_WALK_DEPTH = 40


    def _grok_session_cwd(session_id):
        """The folder Grok stored for this session, or "".

        Every MCP child and hook under `grok agent leader` inherits the leader's
        working directory, so the payload's cwd can be the project that started
        the leader rather than this session's. Grok writes the real one as the
        parent of `~/.grok/sessions/<urlencoded-cwd>/<GROK_SESSION_ID>/`. Same
        rule as `channel_server.grok_session_cwd`, copied because this script
        cannot import the package.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        from pathlib import Path
        from urllib.parse import unquote
        root = Path.home() / ".grok" / "sessions"
        try:
            matches = [p for p in root.glob("*/" + sid) if p.is_dir()]
        except OSError:
            return ""
        if not matches:
            return ""
        try:
            chosen = max(matches, key=lambda p: p.stat().st_mtime)
        except OSError:
            chosen = matches[0]
        return unquote(chosen.parent.name)


    def _read_key(directory):
        """The first non-empty key in `directory`, new folder name first."""
        for name in KEY_DIR_NAMES:
            try:
                with open(os.path.join(directory, name, KEY_FILE_NAME),
                          "r", encoding="utf-8") as fh:
                    found = fh.read(MAX_KEY_BYTES).strip()
            except (OSError, ValueError):
                found = ""
            if found:
                return found
        return ""


    def _project_key(cwd):
        """The enrolment key of the project `cwd` sits in, or ""."""
        try:
            start = os.path.realpath(cwd or os.getcwd())
        except OSError:
            return ""
        try:
            home = os.path.realpath(os.path.expanduser("~"))
        except OSError:
            home = ""
        here = start
        for _ in range(MAX_WALK_DEPTH):
            if not here:
                break
            if here != home:
                found = _read_key(here)
                if found:
                    return found
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
        return ""


    #: Resolved once per run and shared by every message shape. This process
    #: handles exactly one hook event, so "once" is once.
    _KEY = ""

    #: The session's working folder, resolved beside _KEY and stamped on every
    #: message shape for the same reason: one place, so none can forget it.
    _CWD = ""

    #: What started this session, when Dark Army did. Put in the terminal's
    #: environment at the spawn (`origin.py`), read **once** here at import
    #: and carried on every message the way _KEY is — a SessionStart sent
    #: while the daemon was restarting is dropped for ever, and a
    #: start-only stamp would leave that session unattributed for good.
    #: One `os.environ.get` and one dict store per message: no syscall, no
    #: fork, and the pid memo's tuning is untouched. Attribution, never
    #: authorisation: it grants nothing and every gate is elsewhere.
    _ORIGIN = (os.environ.get("BOB_COMPANION_ORIGIN") or "")[:200]


    def _get(hook, *keys, default=""):
        for key in keys:
            if key in hook and hook[key] is not None:
                return hook[key]
        return default

    # What a `StopFailure`'s `error` token means, in the words Claude Code itself
    # uses for it (its own status table, 2.1.261). The hook's `error` is a machine
    # token — `rate_limit`, `billing_error`, `overloaded` — never a sentence, and it
    # used to land on the card verbatim, so a session that had hit its usage limit
    # read `rate_limit` in the panel. The sentence the terminal showed rides in
    # `last_assistant_message` and is preferred when present; this table is the
    # fallback for a token that arrives alone. An unknown token is kept as-is.
    STOP_FAILURE_WORDS = {
        "rate_limit": "Rate limited \u2014 wait and retry",
        "billing_error": "Usage limit reached \u2014 check plan",
        "overloaded": "API overloaded \u2014 wait and retry",
        "server_error": "API unavailable \u2014 retry",
        "authentication_failed": "Login required \u2014 run /login",
        "oauth_org_not_allowed": "Org disabled OAuth \u2014 use API key or ask admin",
        "account_on_hold": "Account on hold \u2014 see detail",
        "invalid_request": "Invalid API request \u2014 see detail",
        "model_not_found": "Model not found",
        "max_output_tokens": "Reply hit the output limit",
        "unknown": "API error",
    }

    # A card message is one line the pane draws in orange; an API error body can be
    # a paragraph. First non-empty line, clamped.
    STOP_FAILURE_MESSAGE_CHARS = 240


    def _stop_failure_line(value):
        if not isinstance(value, str):
            return ""
        for line in value.splitlines():
            line = line.strip()
            if line:
                if len(line) > STOP_FAILURE_MESSAGE_CHARS:
                    line = line[:STOP_FAILURE_MESSAGE_CHARS - 1].rstrip() + "\u2026"
                return line
        return ""


    def stop_failure_message(hook, get):
        """The sentence a `StopFailure` card shows, and the error token beside it.

        `last_assistant_message` is what the terminal printed — for a hit usage
        limit, "You've hit your limit \u00b7 resets 1:40pm" — so it wins. Then the
        raw API message, then the token translated, then the older `stop_reason`
        shape, then the stock words."""
        token = get(hook, "error")
        token = token if isinstance(token, str) else ""
        for key in ("last_assistant_message", "lastAssistantMessage",
                    "error_details", "errorDetails"):
            text = _stop_failure_line(get(hook, key))
            if text:
                return text, token
        if token.strip():
            return STOP_FAILURE_WORDS.get(token, token), token
        reason = get(hook, "stop_reason", "stopReason")
        return (reason if isinstance(reason, str) and reason else "API error"), token


    # The questions a session is about to block on, trimmed to what a row can
    # draw. Kept in step with session_stats.questions_from_tool_input, whose
    # docstring says why the hook is the only timely source: the transcript
    # does not carry the asking turn until somebody has already answered it. A
    # test holds the two ends of this contract together.
    ASK_TOOLS = ("AskUserQuestion", "ask_user_question")
    # Mirrors session_stats.MAX_QUESTION_CHARS, which carries the argument for
    # the number: at 160 a real two-sentence question reached the phone cut
    # mid-clause, under intact option buttons that made the loss invisible.
    MAX_QUESTION_CHARS = 600
    MAX_OPTION_CHARS = 80
    MAX_DETAIL_CHARS = 200
    MAX_OPTIONS = 4
    MAX_QUESTIONS = 4


    def _clip(text, cap):
        """session_stats.clip's twin: cut with a visible `…`, never silently.

        Duplicated rather than imported because this script is installed as a
        standalone file with no path to the daemon package. The contract test
        execs this body and compares its output to the daemon's, so the two
        cannot drift without failing.
        """
        if cap <= 0:
            return ""
        if len(text) <= cap:
            return text
        return text[:cap - 1] + "\u2026"


    def _question_payload(hook):
        """Every question of an AskUserQuestion tool_input, as a list, or None.

        Trimmed *here* rather than on arrival: an option's `description` is
        the interview's reason and rides clipped as `details`; a multi-line
        `preview` is a different layout and does not travel. The tool
        takes one to four questions per call and the dialog answers them in
        order, so the whole list is carried — each element
        {text, options, details, header, id, index, multi_select,
        has_preview}, the id shared because the dialog is one tool call,
        `index` the question's 0-based position, `details` each option's
        `description` in the same order as `options` (empty where none;
        never `preview`), `multi_select` the tool's own `multiSelect` flag
        as a strict bool (anything but `True` reads as pick-one), and
        `has_preview` whether any option carries a non-empty `preview` —
        the terminal draws that pick-one question in a layout where a
        digit only moves focus, so the daemon types an Enter after it.
        """
        tool_input = _get(hook, "tool_input", "toolInput", default=None)
        if not isinstance(tool_input, dict):
            return None
        raw_questions = tool_input.get("questions")
        if not isinstance(raw_questions, list) or not raw_questions:
            return None
        payload = []
        for position, raw in enumerate(raw_questions[:MAX_QUESTIONS]):
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("question") or "").strip()
            if not text:
                continue
            options = []
            details = []
            has_preview = False
            for option in (raw.get("options") or [])[:MAX_OPTIONS]:
                if isinstance(option, dict):
                    label = str(option.get("label") or "").strip()
                    if label:
                        options.append(_clip(label, MAX_OPTION_CHARS))
                        desc = str(option.get("description") or "").strip()
                        details.append(_clip(desc, MAX_DETAIL_CHARS) if desc
                                       else "")
                    preview = option.get("preview")
                    if isinstance(preview, str) and preview.strip():
                        has_preview = True
            payload.append({
                "text": _clip(text, MAX_QUESTION_CHARS),
                "options": options,
                "details": details,
                "header": _clip(str(raw.get("header") or "").strip(),
                                MAX_OPTION_CHARS),
                "id": str(_get(hook, "tool_use_id", "toolUseId") or ""),
                "index": position,
                "multi_select": (raw.get("multiSelect") is True
                                 or raw.get("multi_select") is True),
                "has_preview": has_preview,
            })
        return payload or None


    def _subagent_agent_id(hook):
        unique = _get(hook, "subagent_id", "subagentId", "agent_id", "agentId")
        typed = _get(hook, "subagentType", "subagent_type")
        return str(unique or typed or "")


    def _subagent_type(hook):
        return str(_get(hook, "subagentType", "subagent_type") or "")


    def _parent_session_id(hook):
        return str(_get(hook, "parent_session_id", "parentSessionId") or "")


    def _subagent_message(hook, event, session_id, project, pid, provider):
        agent_id = _subagent_agent_id(hook)
        typed = _subagent_type(hook)
        parent = _parent_session_id(hook)
        msg = _with_common({
            "event": event,
            "agent_id": agent_id,
        }, parent or session_id, project, pid, provider)
        if typed and typed != agent_id:
            msg["subagent_type"] = typed
        if parent:
            msg["parent_session_id"] = parent
        return msg


    def _with_common(msg, session_id, project, pid, provider):
        msg["session_id"] = session_id
        msg["provider"] = provider
        msg["pid"] = pid
        # One place, so every message shape carries it and none can be forgotten.
        msg["key"] = _KEY
        if _ORIGIN:
            msg["origin"] = _ORIGIN
        if _CWD:
            msg["cwd"] = _CWD
        if project:
            msg["project"] = project
        return msg


    # Hook events that become one daemon event naming the tool, and nothing else.
    _TOOL_EVENTS = {
        "PostToolUse": "tool_done",
        "PermissionRequest": "permission",
        "PostToolUseFailure": "tool_failed",
    }


    def _stamped(seen, body, project=None):
        return _with_common(body, seen["session_id"],
                            seen["project"] if project is None else project,
                            seen["pid"], seen["provider"])


    def _card(seen, hook_name, text, extra=None):
        """An `add`: the card for a stopped session, never without a project."""
        body = {"event": "add", "hook": hook_name, "message": text,
                "transcript_path": seen["transcript_path"]}
        body.update(extra or {})
        return _stamped(seen, body, seen["project"] or "unknown")


    def _ended(seen, reason):
        msg = _stamped(seen, {"event": "dismiss", "hook": "SessionEnd"})
        if reason is not None:
            msg["reason"] = reason
        return msg


    def _subagent(seen, event):
        return _subagent_message(seen["hook"], event, seen["session_id"],
                                 seen["project"], seen["pid"], seen["provider"])


    def _on_session_start(hook, seen):
        msg = _stamped(seen, {"event": "session_start"})
        source = _get(hook, "source", default=None)
        if source is not None:
            msg["source"] = source
        return msg


    def _on_tool_start(hook, seen):
        tool = seen["tool_name"]
        msg = _stamped(seen, {"event": "tool_use", "tool_name": tool})
        if tool not in ASK_TOOLS:
            return msg
        asked = _question_payload(hook)
        if asked:
            # `question` stays one object for every installed reader; the
            # whole dialog rides beside it for the readers that want it.
            msg["question"] = {k: v for k, v in asked[0].items() if k != "index"}
            msg["questions"] = asked
        return msg


    def _on_compact(hook, seen):
        return _stamped(seen, {"event": "compact"})


    def _on_stop(hook, seen):
        reason = _get(hook, "reason", default=None)
        if seen["event"] == "Stop" and reason in _STOP_TEARDOWN:
            # Grok's tab-close Stop ends the session outright.
            return _ended(seen, reason)
        return _card(seen, "Stop", "Waiting for input")


    def _on_stop_failure(hook, seen):
        text, token = stop_failure_message(hook, _get)
        return _card(seen, "StopFailure", text, {"error_kind": token} if token else None)


    def _on_notification(hook, seen):
        kind = _get(hook, "notification_type", "notificationType")
        if kind == "permission_prompt":
            tool_input = _get(hook, "tool_input", "toolInput") or {}
            extra = {"event": "permission", "tool_name": seen["tool_name"]}
            message = _get(hook, "message")
            if isinstance(message, str):
                extra["description"] = _clip(message.strip(), MAX_BROKER_DESCRIPTION_CHARS)
            if isinstance(tool_input, dict) and tool_input:
                extra["input_preview"] = _clip(_compact_json(tool_input),
                                               MAX_BROKER_INPUT_CHARS)
            return _stamped(seen, extra)
        if kind == "idle_prompt":
            return _card(seen, "Notification", _get(hook, "message") or "Waiting for input")
        return None


    def _on_prompt(hook, seen):
        return _stamped(seen, {"event": "dismiss", "hook": "UserPromptSubmit"})


    def _on_session_end(hook, seen):
        # A child session ending inside the child is a subagent leaving.
        if _subagent_type(hook):
            return _subagent(seen, "subagent_stop")
        return _ended(seen, _get(hook, "reason", default=None))


    _BUILDERS = {
        "SessionStart": _on_session_start,
        "PreToolUse": _on_tool_start,
        "PreCompact": _on_compact,
        "Stop": _on_stop,
        "StopCancelled": _on_stop,
        "StopFailure": _on_stop_failure,
        "Notification": _on_notification,
        "UserPromptSubmit": _on_prompt,
        "SessionEnd": _on_session_end,
        "SubagentStart": lambda hook, seen: _subagent(seen, "subagent_start"),
        "SubagentStop": lambda hook, seen: _subagent(seen, "subagent_stop"),
    }


    def hook_to_message(hook):
        """The daemon message this hook payload stands for, or None.

        Also settles, once for the run, the project key and working folder
        every message carries (_KEY, _CWD), and finds the session's pid."""
        global _KEY, _CWD
        raw = _get(hook, "hook_event_name", "hookEventName")
        event = _EVENT_ALIASES.get(raw, raw)
        sid = _get(hook, "session_id", "sessionId")
        grok_id = os.environ.get("GROK_SESSION_ID")
        codex_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
        cwd = _get(hook, "cwd")
        # Under Grok's shared leader the payload's cwd is the leader's own
        # project; the session's folder comes from Grok's roster instead, or
        # the key read would admit the wrong project.
        if grok_id:
            cwd = _grok_session_cwd(grok_id) or cwd
        _KEY = _project_key(cwd)
        _CWD = cwd or ""
        pid, walked = (None, "codex") if codex_id else _session_pid_cached(sid, bool(grok_id))
        seen = {
            "hook": hook,
            "event": event,
            "session_id": sid,
            # The trailing slash is stripped first: basename("/a/b/") is "".
            "project": os.path.basename(cwd.rstrip("/")) if cwd else "",
            "pid": pid,
            "provider": "codex" if codex_id else ("grok" if grok_id else walked),
            "tool_name": _get(hook, "tool_name", "toolName"),
            "transcript_path": _get(hook, "transcript_path", "transcriptPath"),
        }
        if event in _TOOL_EVENTS:
            return _stamped(seen, {"event": _TOOL_EVENTS[event], "tool_name": seen["tool_name"]})
        build = _BUILDERS.get(event)
        return build(hook, seen) if build else None


    def _wire(msg):
        """One message as the daemon reads it: a JSON line."""
        return (json.dumps(msg) + "\\n").encode("utf-8")


    def _compact_json(obj):
        try:
            return json.dumps(obj, separators=(",", ":"),
                              ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return ""


    def _broker_description(tool_input):
        """One human line about what the tool wants to do.

        The path or the command, when the tool named one — that is the whole
        of what the person on the phone needs to decide — and the compact
        arguments otherwise. Untrusted text all the way to whatever renders
        it, and clamped here *and* daemon-side: this script is a copy on disk
        and may be older than the daemon reading it.
        """
        for key in ("file_path", "path", "command"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value.strip(), MAX_BROKER_DESCRIPTION_CHARS)
        return _clip(_compact_json(tool_input), MAX_BROKER_DESCRIPTION_CHARS)


    def _broker_exchange(msg):
        """One connection, one line up, one line back — or None.

        None means "Dark Army said nothing useful": not listening, no reply, an
        empty line, something that is not a JSON object. Every one of those
        ends the broker silently, which leaves the terminal's own dialog
        exactly as it is today.
        """
        try:
            with _hook_connect(BROKER_SOCKET_TIMEOUT) as sock:
                sock.settimeout(BROKER_SOCKET_TIMEOUT)
                sock.sendall(_wire(msg))
                buf = b""
                while b"\\n" not in buf and len(buf) < 65536:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except (OSError, ValueError):
            return None
        line = buf.split(b"\\n", 1)[0].decode("utf-8", "replace").strip()
        if not line:
            return None
        try:
            reply = json.loads(line)
        except ValueError:
            return None
        return reply if isinstance(reply, dict) else None


    def _broker_decision(behavior):
        """The hook's own answer to the dialog.

        The behaviour and nothing else: no rule update, no rewritten input.
        An allow from Dark Army is an allow *once* — "always allow" from a surface
        showing a clamped preview is the wrong affordance — and this stdout
        contract is one decision object or nothing at all.
        """
        decision = {"behavior": behavior}
        if behavior == "deny":
            decision["message"] = "Denied from Dark Army"
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            }
        }, separators=(",", ":"))


    def _run_permission_broker(hook, base):
        """Offer this permission ask to Dark Army, and answer the dialog if Dark Army
        answers first.

        The terminal's dialog is up the whole time and either answer wins,
        so every failure here is silence: no reply, a refused hold, a socket
        that went away, or the deadline — all of them exit 0 having printed
        nothing, and the person at the desk never knows Dark Army was asked.

        Returns True where Dark Army answered the ask — a hold, a refusal, a
        verdict, a lost socket mid-poll — and False only where **nothing**
        came back from the first exchange. That one case is indistinguishable
        from a daemon too old to know the verb, whose `LIFECYCLE_EVENTS` has
        no `permission_ask` and which therefore also never ran the legacy
        `permission` state effect; the caller sends the legacy message
        instead, so a new script against an old daemon is exactly today
        rather than an ask that vanishes from Dark Army altogether. A daemon that
        *did* reply has already run that state effect itself, so an explicit
        `{"hold": 0}` must not be followed by a second message.
        """
        import uuid
        deadline = time.monotonic() + BROKER_WAIT_SECONDS
        request_id = "hook-" + uuid.uuid4().hex
        claim = uuid.uuid4().hex
        tool_input = _get(hook, "tool_input", "toolInput", default=None)
        if not isinstance(tool_input, dict):
            tool_input = {}
        ask = dict(base)
        ask["event"] = "permission_ask"
        ask["request_id"] = request_id
        ask["claim"] = claim
        ask["agent_type"] = _get(hook, "agent_type", "agentType") or ""
        ask["description"] = _broker_description(tool_input)
        ask["input_preview"] = _clip(_compact_json(tool_input),
                                     MAX_BROKER_INPUT_CHARS)
        reply = _broker_exchange(ask)
        if not reply:
            return False
        try:
            hold = float(reply.get("hold") or 0)
        except (TypeError, ValueError):
            return True
        if hold <= 0:
            return True
        poll = dict(base)
        poll["event"] = "permission_poll"
        poll["request_id"] = request_id
        poll["claim"] = claim
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(BROKER_POLL_SECONDS, remaining))
            if time.monotonic() >= deadline:
                return True
            answer = _broker_exchange(poll)
            if answer is None:
                return True
            verdict = answer.get("verdict")
            if verdict in ("allow", "deny"):
                sys.stdout.write(_broker_decision(verdict))
                return True
            if verdict == "gone":
                return True


    def main():
        try:
            raw = sys.stdin.read()
            if not raw.strip():
                sys.exit(0)
            hook = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit(1)

        msg = hook_to_message(hook)
        if msg is None:
            sys.exit(0)

        # The one branch that waits for an answer. `hook_to_message` has
        # already run, so the enrolment key, cwd, pid and provider on `msg`
        # are the same ones every other event carries — the broker only
        # changes the verb and adds its own three fields. Claude only: the
        # decision object this branch can print is Claude Code's own stdout
        # contract, and `_EVENT_ALIASES` maps Grok's `permission_request`
        # onto the same name, so a future Grok build firing it would
        # otherwise be handed a Claude-shaped `hookSpecificOutput` it never
        # asked for. And a broker that heard nothing at all falls through
        # to the legacy message: that is the older-daemon case, which must
        # be today's behaviour rather than an ask that reaches Dark Army not at
        # all.
        raw_event = _get(hook, "hook_event_name", "hookEventName")
        if (_EVENT_ALIASES.get(raw_event, raw_event) == "PermissionRequest"
                and msg.get("provider") in ("claude", "codex")):
            if _run_permission_broker(hook, msg):
                return

        delivered = False
        try:
            with _hook_connect(3.0) as sock:
                sock.sendall(_wire(msg))
                delivered = True
        except (OSError, ValueError):
            pass

        # Ask for the TL;DR and the completion report only when someone just
        # took delivery: no daemon means no row, and a standing instruction to
        # annotate for nobody is context spent on silence. Claude only —
        # Grok's hook contract does not promise that stdout becomes context.
        if (delivered and msg.get("event") == "session_start"
                and msg.get("provider") != "grok"):
            print(TLDR_HINT)
            print(WORK_REPORT_HINT)
            # SEARCH_SCOPE_HINT, read fresh from the search-scope file.
            print(_search_scope_hint())


    if __name__ == "__main__":
        main()
''')

# The script is chmod +x with a python3 shebang, but Grok (and Claude) run
# this command in the session's environment. A session Dark Army started from the
# frozen .app inherits py2app's PYTHONHOME; `python3` then dies with
# "Python path configuration" *before* the file is parsed — every PreToolUse
# shows as a failed hook even when the tool itself succeeded. `env -u`
# strips those names so the interpreter boots against the real prefix.
HOOK_COMMAND = (
    "/usr/bin/env "
    + " ".join(f"-u {name}" for name in PY2APP_ENV_VARS)
    + f" python3 {NOTIFY_SCRIPT_PATH}"
)

# The shunt guard — the second standalone script, installed beside the notify
# script by `install_shunt_script` under the same rules (stdlib only, any
# `python3` including 3.9, no `dark_army_*` import, temp sibling +
# `os.replace`, skipped when current). Unlike `NOTIFY_SCRIPT` it lives as a
# real module, `shunt_hook.py` next to this file, so ruff reads it, the tests
# import its functions, and a regex is written once rather than escaped
# twice; `SHUNT_SCRIPT` is that file's bytes, copied verbatim on install
# (py2app ships the package's sources, so the frozen app finds it too). It
# runs as a PreToolUse hook on all three assistants and refuses a whole-file
# read over the delegation threshold, naming the shunt skill instead. Every
# decision it cannot make in time is an allow: bounded reads, no socket, no
# subprocess, and any failure exits 0 having printed nothing.
# `test_shunt_script.py` pins the 3.9 parse and the stdlib-only imports.
SHUNT_SCRIPT = Path(__file__).with_name("shunt_hook.py").read_text(encoding="utf-8")

SHUNT_COMMAND = (
    "/usr/bin/env "
    + " ".join(f"-u {name}" for name in PY2APP_ENV_VARS)
    + f" python3 {SHUNT_SCRIPT_PATH}"
)
# The tools the guard watches, by their names on the three assistants:
# Claude's `Read` and `Bash`, Grok's `read_file` and `bash`, Codex's `shell`.
SHUNT_MATCHER = "Read|Bash|read_file|bash|shell"
# The read-only roles by the tail of their `agent_type`, mirrored in the
# guard string above; a reviewer is never refused a read.
SHUNT_REVIEWER_SUFFIXES = ("-verifier", "-bug-auditor",
                           "-integration-reviewer", "-security-reviewer")

def _command_group(command=HOOK_COMMAND, matcher=None, **hook_keys) -> dict:
    """One settings group running one command, optionally under a matcher.
    `hook_keys` are extra keys on the hook itself (a timeout)."""
    hook = {"type": "command", "command": command, **hook_keys}
    return {"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}


# What Dark Army registers in Claude Code's settings, one row per group:
# (event, matcher, command, extra hook keys). Events keep their row order,
# and so do the groups within an event. `HOOKS_CONFIG` is built from these
# rows once, at import; `tests/data/hooks-config.golden.json` pins the result.
#
# Notes on individual rows:
#
# - Notification, `permission_prompt`: Grok has no PermissionRequest event;
#   a permission UI waiting on the human arrives as this notification type.
#
# - PreToolUse has two groups, and they stay two. The first is the unmatched
#   notify entry every other event has; the second is the shunt guard on the
#   read and shell tools alone. A separate Dark Army-managed group rather than a
#   second hook inside the notify group keeps `_is_our_managed_group`'s
#   "every hook is ours" true for both, leaves the notify entry
#   byte-identical, and keeps a user's own group sharing the shunt matcher
#   whole, as today.
#
# - PostToolUse only for the ask-user tools: all it is for is noticing that
#   a question has been answered, so the waiting row can move on. Every other
#   tool's PostToolUse would be a second event per call telling us nothing.
#
# - PostToolUseFailure: a tool that failed outright (a shell command exiting
#   non-zero is not one) makes the row read as confused.
#
# - PermissionRequest: the session is stopped on a "may I?" dialog.
#   The only entry with a timeout of its own: this hook may *hold*, offering
#   the ask to Dark Army while the terminal's dialog runs concurrently, and the
#   number is pinned above the script's own BROKER_WAIT_SECONDS so the
#   script always exits on its deadline rather than being killed mid-write.
#   Pinned rather than inherited: the CLI's default is not ours to depend on.
#
#   It moves *with* BROKER_WAIT_SECONDS or not at all. Raising the script's
#   deadline and leaving this behind caps the real hold at this number with
#   nothing anywhere saying so — the CLI kills the hook, the script prints
#   nothing, and the daemon reaps the row a poll lapse later. 60s of
#   headroom above the script's own deadline is what keeps this entry's
#   stated invariant true; `test_permission_broker.py` pins the relation.
#
#   **The invariant has one hole, and it is here so the next reader finds it
#   before it finds them.** It holds for a group Dark Army owns. It does not hold
#   for a group the user *shares* with us — one where their own command sits
#   in this entry's `hooks` list beside ours. `_is_our_managed_group` requires
#   *every* hook in the group to be ours, so a shared group is not ours;
#   `_our_group_is_current` then judges a shared group on command presence
#   alone, deliberately, because the merge keeps such a group whole and
#   calling it outdated would only append a duplicate that fires the script
#   twice. The consequence: on such an install `are_hooks_installed()` keeps
#   answering True while the group still carries whatever timeout it was
#   written with — `600` for anything installed before this change — silently
#   capping a 1800s hold at ten minutes, by a route the relation test in
#   `test_permission_broker.py` cannot see (it reads HOOKS_CONFIG, not the
#   settings file on disk). The failure is quiet and survivable: the CLI kills
#   the hook at 600s, the script prints nothing, and the daemon reaps the row
#   a poll lapse later, which is exactly today's behaviour. Fixing it means
#   changing what Dark Army may rewrite inside a group it does not own, which is a
#   decision of its own and not this entry's to make.
_HOOK_ROWS = (
    ("SessionStart", None, HOOK_COMMAND, {}),
    ("Stop", None, HOOK_COMMAND, {}),
    ("StopFailure", None, HOOK_COMMAND, {}),
    ("Notification", "idle_prompt", HOOK_COMMAND, {}),
    ("Notification", "permission_prompt", HOOK_COMMAND, {}),
    ("UserPromptSubmit", None, HOOK_COMMAND, {}),
    ("PreToolUse", None, HOOK_COMMAND, {}),
    ("PreToolUse", SHUNT_MATCHER, SHUNT_COMMAND, {}),
    ("PostToolUse", POST_TOOL_USE_MATCHER, HOOK_COMMAND, {}),
    ("PermissionRequest", None, HOOK_COMMAND, {"timeout": 1860}),
    ("PostToolUseFailure", None, HOOK_COMMAND, {}),
    ("PreCompact", None, HOOK_COMMAND, {}),
    ("SessionEnd", None, HOOK_COMMAND, {}),
    ("SubagentStart", None, HOOK_COMMAND, {}),
    ("SubagentStop", None, HOOK_COMMAND, {}),
)


def _table_from_rows(rows) -> dict:
    table: dict = {}
    for event, matcher, command, hook_keys in rows:
        table.setdefault(event, []).append(_command_group(command, matcher, **hook_keys))
    return table


HOOKS_CONFIG = _table_from_rows(_HOOK_ROWS)


def _backup_unusable_settings(why: str) -> None:
    """Preserve a settings.json a write path found unreadable.

    The name is timestamped rather than a fixed .bak: a second failure would
    otherwise overwrite the first backup, destroying the only surviving copy
    of the user's original config. Best-effort and never fatal — failing to
    back up must not stop the user installing hooks, but it should be loud.

    Deduped by content: a broken file used to mint a fresh .bak on *every
    read* (`is_title_env_installed` runs on every launch), filling the folder
    with identical copies. One backup per distinct content is the whole
    point of a backup, so an existing .bak with these exact bytes means skip."""
    try:
        current = CLAUDE_SETTINGS_PATH.read_bytes()
        for existing in CLAUDE_SETTINGS_PATH.parent.glob(
                CLAUDE_SETTINGS_PATH.name + ".*.bak"):
            try:
                if existing.read_bytes() == current:
                    return
            except OSError:
                continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = CLAUDE_SETTINGS_PATH.with_name(
            f"{CLAUDE_SETTINGS_PATH.name}.{stamp}.bak"
        )
        shutil.copy2(CLAUDE_SETTINGS_PATH, backup)
        logger.warning(
            "%s in %s — it will be replaced. Previous contents saved to %s",
            why, CLAUDE_SETTINGS_PATH, backup,
        )
    except OSError:
        logger.warning(
            "%s in %s and it could not be backed up; it will be replaced",
            why, CLAUDE_SETTINGS_PATH, exc_info=True,
        )


def read_claude_settings(for_write: bool = False) -> tuple[bool, dict]:
    """``(ok, settings)`` for a read-modify-write of ~/.claude/settings.json.

    ``ok`` is False when the file exists but could not be read as an object.
    **A caller that is about to write must refuse on False**, exactly as
    `_set_grok_title_writer` does with its own ``ok``: the {} returned
    alongside it is not the user's settings, and writing it back replaces the
    whole file — their model, permissions, statusLine and their own hooks. A
    backup is taken, but a backup is a way to recover from data loss, not a
    licence to cause it, and this runs unattended on every launch.

    ``for_write`` marks a caller that intends to write the file back. Only
    those take the backup on an unreadable file: a *reader*
    (`is_title_env_installed`, `is_statusline_installed`) runs on every launch
    and was minting a fresh .bak per read while never being in a position to
    damage anything.

    A missing file is ``(True, {})``: nothing to lose, and building one is the
    point of the install path.
    """
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CLAUDE_SETTINGS_PATH.exists():
        return True, {}
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        if for_write:
            _backup_unusable_settings("Could not parse JSON")
        return False, {}
    if not isinstance(settings, dict):
        if for_write:
            _backup_unusable_settings("Top-level value is not an object")
        return False, {}
    return True, settings


def load_claude_settings() -> dict:
    """The settings, or {} if they could not be read. For *readers* only —
    a write-back caller must use `read_claude_settings` and honour its ``ok``,
    or an unreadable file is silently rewritten as an empty one."""
    return read_claude_settings()[1]


def write_claude_settings(settings: dict) -> None:
    """Replace settings.json atomically.

    Write-then-rename rather than `write_text`: a truncating write that dies
    partway (full disk, crash, the app being quit) leaves the user with a
    half-written settings.json, which Claude Code cannot parse and which the
    next `read_claude_settings` will decline to touch — the file is stuck
    broken until somebody edits it by hand. `os.replace` is atomic on the same
    filesystem, so the file is either the old one or the new one. A unique
    `mkstemp` sibling rather than the fixed `.tmp` name, so two processes
    saving at once cannot interleave writes into one temp file.
    """
    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(CLAUDE_SETTINGS_PATH.stat().st_mode)
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=str(CLAUDE_SETTINGS_PATH.parent),
                                    prefix=CLAUDE_SETTINGS_PATH.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2) + "\n")
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, CLAUDE_SETTINGS_PATH)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def script_current(path: Path, text: str) -> bool:
    """True when the installed copy already has this exact content and is
    executable — the skip that keeps every launch from rewriting a file a hook
    may be executing right now. Content, not a version marker: a marker can be
    right while the body is wrong, and comparing the body repairs both."""
    try:
        if path.read_text(encoding="utf-8") != text:
            return False
        return bool(path.stat().st_mode & stat.S_IXUSR)
    except (OSError, UnicodeDecodeError):
        return False


def write_script_atomic(path: Path, text: str, mode: int = 0o755) -> None:
    """Write an executable script via a unique temp sibling + `os.replace`.

    Never a truncating `write_text` on the live path: these scripts are exec'd
    by name on every hook event, and a hook firing mid-write runs a truncated
    script. The rename is atomic, so the file is always either the old script
    or the new one. A unique `mkstemp` name rather than a fixed `.tmp` sibling,
    so two writers cannot trample each other's half-written temp file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install_notify_script() -> None:
    """Write the standalone notify script to ~/.dark-army/dark-army-notify.

    Skipped when the installed copy is already byte-for-byte current: this runs
    on every launch and on several preference toggles, and an unchanged file
    does not need a rewrite a concurrently firing hook could catch halfway.
    """
    ensure_state_dir()
    if script_current(NOTIFY_SCRIPT_PATH, NOTIFY_SCRIPT):
        return
    write_script_atomic(NOTIFY_SCRIPT_PATH, NOTIFY_SCRIPT)
    logger.info("Wrote the hook handler to %s", NOTIFY_SCRIPT_PATH)


def install_shunt_script() -> None:
    """Write the shunt guard to ~/.dark-army/dark-army-shunt —
    `install_notify_script`'s discipline exactly: skipped when the installed
    copy is byte-for-byte current and executable, else a temp sibling and
    `os.replace`, so a hook firing mid-install never runs half a script."""
    ensure_state_dir()
    if script_current(SHUNT_SCRIPT_PATH, SHUNT_SCRIPT):
        return
    write_script_atomic(SHUNT_SCRIPT_PATH, SHUNT_SCRIPT)
    logger.info("Installed shunt guard: %s", SHUNT_SCRIPT_PATH)


# The /ship close-out script. Unlike the notify script it is a file, not an
# embedded string: it lives at the repo root (`.claude/skills/ship/`) so every
# project can carry a byte copy, and build.sh bundles it under this resource
# name so an installed app can install it without a source checkout.
CLOSE_OUT_SOURCE_NAME = "dark-army-close-out.sh"
CLOSE_OUT_REPO_RELATIVE = Path(".claude") / "skills" / "ship" / "close-out.sh"


def close_out_source() -> Optional[Path]:
    """Where the current close-out script is: the bundle's Resources first
    (build.sh copies it there), then the source checkout (`find_repo_root`,
    which reaches an installed bundle through its `repo-root` stamp). None
    on a clean install with neither — the caller leaves any existing copy
    alone rather than guess."""
    from dark_army_menubar import dev_build

    bundle = dev_build.bundle_path()
    if bundle is not None:
        hit = bundle / "Contents" / "Resources" / CLOSE_OUT_SOURCE_NAME
        if hit.is_file():
            return hit
    root = dev_build.find_repo_root()
    if root is not None:
        hit = root / CLOSE_OUT_REPO_RELATIVE
        if hit.is_file():
            return hit
    return None


def install_close_out_script() -> None:
    """Copy the /ship close-out script to ~/.dark-army/dark-army-close-out.

    `install_notify_script`'s discipline: every launch, compared by content
    (a stale installed copy is worse than none — every project's shim hands
    off to it), skipped when byte-equal and executable, written atomically
    otherwise. No source found means the existing copy is left untouched and
    one log line says so; it is never unlinked.
    """
    ensure_state_dir()
    src = close_out_source()
    if src is None:
        logger.info("close-out script source not found; leaving %s alone",
                    CLOSE_OUT_SCRIPT_PATH)
        return
    text = src.read_text(encoding="utf-8")
    if script_current(CLOSE_OUT_SCRIPT_PATH, text):
        return
    write_script_atomic(CLOSE_OUT_SCRIPT_PATH, text)
    logger.info("Installed close-out script: %s", CLOSE_OUT_SCRIPT_PATH)


def _matcher_of(entry: dict):
    """A group's matcher, with "matches everything" always spelled None.

    A group without the key and one with `"matcher": ""` both fire on every
    tool, so for finding a group they are the same group.
    """
    return (entry.get("matcher") or None) if isinstance(entry, dict) else None


def _group_hooks(entry) -> list:
    """The hook dicts inside a group, or [] for anything that is not one."""
    listed = entry.get("hooks") if isinstance(entry, dict) else None
    return [h for h in listed if isinstance(h, dict)] if isinstance(listed, list) else []


def _only_commands(entry, test) -> bool:
    """True for a group holding hooks, every one of them a dict whose command
    passes `test`. A single foreign hook (or a non-dict) makes it False."""
    listed = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(listed, list) or not listed:
        return False
    return len(_group_hooks(entry)) == len(listed) and all(
        test(h.get("command", "")) for h in listed)


def _command_is_ours(command) -> bool:
    """True only if a hook command actually invokes our notify script or the
    shunt guard.

    Matches the current `HOOK_COMMAND` (the `env -u … python3 <path>` wrapper)
    and the bare path an older install wrote, so an upgrade replaces the
    leaked-PYTHONHOME command rather than adding a second group, plus
    `SHUNT_COMMAND` and its bare path for the same reason: the shunt group is
    Dark Army's to prune and rewrite. A command that merely contains the path
    (`cat <path>`) is not ours.
    """
    if not isinstance(command, str):
        return False
    for ours in (HOOK_COMMAND, str(NOTIFY_SCRIPT_PATH),
                 SHUNT_COMMAND, str(SHUNT_SCRIPT_PATH)):
        if command == ours or command.startswith(ours + " "):
            return True
    return False


def _group_runs_our_command(entry: dict) -> bool:
    """True when at least one hook in the group is one of ours."""
    return any(_command_is_ours(h.get("command", "")) for h in _group_hooks(entry))


def _is_our_managed_group(entry: dict) -> bool:
    """True for a group we wrote: it has hooks and all of them are ours.

    Only such a group may be dropped and rewritten on install. A group where
    the person's own command sits beside ours is theirs as much as ours, and
    is left whole."""
    return _only_commands(entry, _command_is_ours)


def _our_hook_present(existing_entries, our_matcher) -> bool:
    """Whether one of the groups under `our_matcher` already runs our command."""
    return isinstance(existing_entries, list) and any(
        _matcher_of(group) == our_matcher and _group_runs_our_command(group)
        for group in existing_entries)


def _our_hook_is_current(existing_hook, our_hook) -> bool:
    """True if an installed hook dict of ours says exactly what `HOOKS_CONFIG` says.

    Every key on either side, not just the command: the `timeout` on the
    PermissionRequest entry is the whole point of pinning it, and an install
    carrying the same command with no timeout has to read as outdated or the
    number never reaches a settings file that already exists.
    """
    if not isinstance(existing_hook, dict) or not isinstance(our_hook, dict):
        return False
    return all(existing_hook.get(key) == our_hook.get(key)
               for key in set(our_hook) | set(existing_hook))


def _our_group_is_current(existing_entries, our_entry) -> bool:
    """True if a group with our matcher already carries our hooks as configured.

    Two rules, because there are two kinds of group. One we *own* (every hook in
    it runs our command) is compared against `HOOKS_CONFIG` field for field, so
    a config change marks the install outdated and `_merge_hook_config` — which
    drops and rewrites our managed groups — puts the new shape in. One the user
    *shares* with us, mixing their own command into the group, is judged the old
    way, on command presence alone: we do not own it, the merge deliberately
    keeps it whole, and calling it outdated would only append a duplicate group
    that fires our script twice.

    The whole list is scanned rather than answered by the first match: a stale
    managed group can sit beside a shared one, and short-circuiting on the
    shared group would leave the stale one — the very install this check
    exists to rewrite — reading as current for ever. So a stale managed group
    is decisive, and a shared group only records that our command is present.
    """
    if not isinstance(existing_entries, list):
        return False
    our_matcher = _matcher_of(our_entry)
    our_hooks = our_entry.get("hooks") if isinstance(our_entry, dict) else None
    our_hooks = our_hooks if isinstance(our_hooks, list) else []
    found = False
    for entry in existing_entries:
        if _matcher_of(entry) != our_matcher or not _group_runs_our_command(entry):
            continue
        if not _is_our_managed_group(entry):
            found = True
            continue
        their_hooks = entry.get("hooks")
        their_hooks = their_hooks if isinstance(their_hooks, list) else []
        if len(their_hooks) != len(our_hooks) or not all(
                _our_hook_is_current(h, o)
                for h, o in zip(their_hooks, our_hooks)):
            return False
        found = True
    return found


def _leftover_group(group, wanted_matchers) -> bool:
    """A group an install would remove: ours under a matcher `config` no
    longer asks for."""
    return _is_our_managed_group(group) and _matcher_of(group) not in wanted_matchers


def _hooks_dict_is_current(hooks, config) -> bool:
    """Whether an installed event → groups map needs no install at all: every
    group `config` names is there as configured, and nothing an install
    would prune is left beside them."""
    if not isinstance(hooks, dict):
        return False
    for event, wanted in config.items():
        installed = hooks.get(event, [])
        if not all(_our_group_is_current(installed, group) for group in wanted):
            return False
        matchers = {_matcher_of(group) for group in wanted}
        if isinstance(installed, list) and any(
                _leftover_group(group, matchers) for group in installed):
            return False
    return True


def grok_hooks_config() -> dict:
    """Claude's hook set plus Grok-only events (StopCancelled).

    Minus the `timeout` Claude's PermissionRequest entry pins: Grok never
    fires that event, so the key would buy nothing, and it is untested
    whether Grok's hook loader tolerates an unknown key on a hook dict — a
    strict validator could reject the whole file and take every other event
    down with it.
    """
    cfg = copy.deepcopy(HOOKS_CONFIG)
    cfg["StopCancelled"] = [_command_group()]
    for groups in cfg.values():
        for group in groups:
            for hook in group.get("hooks", []):
                hook.pop("timeout", None)
    return cfg


def codex_hooks_config() -> dict:
    """The shunt guard and the one-shot permission broker for Codex."""
    group = copy.deepcopy(HOOKS_CONFIG["PreToolUse"][1])
    permission = copy.deepcopy(HOOKS_CONFIG["PermissionRequest"][0])
    return {"PreToolUse": [group], "PermissionRequest": [permission]}


def are_codex_hooks_installed() -> bool:
    """True if ~/.codex/hooks.json carries both owned Codex groups."""
    if not CODEX_HOOKS_PATH.exists():
        return False
    try:
        data = json.loads(CODEX_HOOKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    return _hooks_dict_is_current(data.get("hooks", {}), codex_hooks_config())


def are_claude_hooks_installed() -> bool:
    """True only if every Dark Army hook is registered in Claude settings."""
    if not CLAUDE_SETTINGS_PATH.exists():
        return False
    try:
        settings = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(settings, dict):
        return False
    return _hooks_dict_is_current(settings.get("hooks", {}), HOOKS_CONFIG)


def are_grok_hooks_installed() -> bool:
    """True if ~/.grok/hooks/dark-army.json already has our current config."""
    if not GROK_HOOKS_PATH.exists():
        return False
    try:
        data = json.loads(GROK_HOOKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    return _hooks_dict_is_current(data.get("hooks", {}), grok_hooks_config())


def are_hooks_installed() -> bool:
    """Whether Claude Code, Grok and Codex all carry our current hooks and
    Grok its rules file, so the launch needs no install.

    "Current" means field for field under the right matcher: a group of ours
    under a matcher we have since changed reads as outdated, and the app then
    runs `install_hooks()` again. Checked in that order, stopping at the
    first that is not."""
    checks = (are_claude_hooks_installed, are_grok_hooks_installed,
              are_codex_hooks_installed, are_grok_rules_installed)
    return all(check() for check in checks)


def _merge_hook_config(hooks: dict, config: dict) -> dict:
    """`hooks` with our groups in place and nobody else's disturbed.

    Per event: the groups we wrote are dropped, everything else stays in its
    order, and each group `config` names is appended unless a kept group under
    its matcher already runs our command (a group the person shares with us
    keeps serving)."""
    merged = hooks if isinstance(hooks, dict) else {}
    for event, wanted in config.items():
        installed = merged.get(event)
        kept = [group for group in (installed if isinstance(installed, list) else [])
                if not _is_our_managed_group(group)]
        for group in wanted:
            if not _our_hook_present(kept, _matcher_of(group)):
                kept.append(copy.deepcopy(group))
        merged[event] = kept
    return merged


# Claude Code's own terminal-title writer, and the only way to switch it off.
# Set in the `env` block of ~/.claude/settings.json rather than in a shell
# profile: it belongs to the same file we already own hooks in, it survives
# however the user launches `claude` (VS Code's shell, a login shell, a task),
# and turning the preference off can take it back out again — which editing
# someone's .zshrc could not honestly promise.
TITLE_ENV_KEY = "CLAUDE_CODE_DISABLE_TERMINAL_TITLE"


def is_title_env_installed() -> bool:
    """True if Claude Code has been told to leave the terminal title alone."""
    env = load_claude_settings().get("env")
    return isinstance(env, dict) and str(env.get(TITLE_ENV_KEY, "")) == "1"


def set_title_env() -> None:
    """Take the terminal title away from the harnesses.

    Claude: set the env key once and leave it. Grok: write ``enabled = false``
    under the title table when absent. Codex: own the marked
    ``terminal_title = []`` line only when not user-owned.

    Takes effect on the next session — all three harnesses read this at
    startup, and nothing here can reach one already running.
    """
    _set_claude_title_env()
    _set_grok_title_writer()
    _set_codex_title_writer()


def _set_claude_title_env() -> None:
    ok, settings = read_claude_settings(for_write=True)
    if not ok:
        # The Grok half of this pair has always had this guard. Without it the
        # tab badge — a cosmetic feature, running on every launch — is enough
        # to reduce an unreadable settings.json to a single env key.
        logger.info("settings.json is not readable; not touching %s", TITLE_ENV_KEY)
        return
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    if str(env.get(TITLE_ENV_KEY, "")) == "1":
        return
    env[TITLE_ENV_KEY] = "1"
    settings["env"] = env
    write_claude_settings(settings)
    logger.info("Set %s in %s", TITLE_ENV_KEY, CLAUDE_SETTINGS_PATH)


# Grok's own tab writer. Default true; we turn it off so the daemon can name
# the tab without fighting a spinner rewrite every few hundred milliseconds.
_GROK_TITLE_TABLE = re.compile(r"(?m)^\[ui\.notifications\.title\][ \t]*$")
_GROK_TITLE_ENABLED = re.compile(r"(?m)^enabled\s*=\s*(true|false)[ \t]*(?:#.*)?$")


def _grok_title_enabled(text: str):
    """``(ok, value)``. ``value`` is True/False, or None when the key is absent.

    ``ok`` is False when the file is not TOML — the caller must not rewrite
    a file it could not read, or one boolean would trash the user's config.
    """
    if not text.strip():
        return True, None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False, None
    title = ((data.get("ui") or {}).get("notifications") or {}).get("title")
    if not isinstance(title, dict) or "enabled" not in title:
        return True, None
    return True, bool(title["enabled"])


def _grok_title_declared(text: str) -> bool:
    """Is ``ui.notifications.title`` declared at all, however it is spelled?

    Separate from `_grok_title_enabled`, which answers only about the
    ``enabled`` key: a `title = {}` inline table or a `[ui.notifications]`
    section with a dotted `title.foo` both declare the table while leaving
    ``enabled`` absent. Appending our own `[ui.notifications.title]` header in
    that case is a duplicate declaration, and Grok cannot load the file.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    notifications = (data.get("ui") or {}).get("notifications")
    return isinstance(notifications, dict) and "title" in notifications


def _table_span(text: str, header: re.Pattern) -> tuple[int, int, int] | None:
    """``(header_start, body_start, body_end)`` for a TOML table, or None."""
    match = header.search(text)
    if match is None:
        return None
    body_start = match.end()
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    nxt = re.search(r"(?m)^\[", text[body_start:])
    body_end = body_start + nxt.start() if nxt else len(text)
    return match.start(), body_start, body_end


def _set_grok_title_writer(path: Path | None = None) -> None:
    """Write ``enabled = false`` under the title table when absent."""
    dest = path if path is not None else GROK_CONFIG_PATH
    try:
        text = dest.read_text(encoding="utf-8") if dest.exists() else ""
    except OSError:
        logger.info("Could not read Grok config", exc_info=True)
        return
    ok, current = _grok_title_enabled(text)
    if not ok:
        logger.info("Grok config.toml is not parseable; not touching title.enabled")
        return
    if current is False:
        return

    span = _table_span(text, _GROK_TITLE_TABLE)
    if span is None:
        if _grok_title_declared(text):
            # The table is declared — just not
            # as a literal `[ui.notifications.title]` header line. It can be a
            # dotted key, an inline table, or nested under `[ui]` / `[ui.notifications]`.
            # Appending our own header in that case declares the same table
            # twice, which is a TOML error: Grok then cannot load its config at
            # all, and this runs unattended on every launch. Editing an arbitrary
            # nesting with regexes is not something to guess at, so we decline
            # and leave the tab to Grok's own writer.
            logger.info(
                "Grok's title.enabled is declared outside a [ui.notifications.title] "
                "header; leaving %s alone", dest,
            )
            return
        pad = "" if not text or text.endswith("\n") else "\n"
        new = text + pad + "\n[ui.notifications.title]\nenabled = false\n"
    else:
        _header_start, body_start, body_end = span
        body = text[body_start:body_end]
        if _GROK_TITLE_ENABLED.search(body):
            body = _GROK_TITLE_ENABLED.sub("enabled = false", body, count=1)
        else:
            body = "enabled = false\n" + body
        new = text[:body_start] + body + text[body_end:]
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new, encoding="utf-8")
    except OSError:
        logger.info("Could not write Grok title.enabled", exc_info=True)
        return
    logger.info("Disabled title.enabled in %s", dest)


# Codex's terminal-title list. An empty list disables its own writer, leaving
# the tty to Dark Army. Unlike Claude's JSON setting this is a user-formatted TOML
# file, so Dark Army owns exactly one marked line and declines every ambiguous shape.
_CODEX_TUI_TABLE = re.compile(r"(?m)^\[tui\][ \t]*(?:#.*)?$")
_CODEX_TITLE_MARKER = "terminal_title = [] # managed by Bob Companion"
_CODEX_MARKED_TITLE = re.compile(
    r"(?m)^[ \t]*terminal_title[ \t]*=[ \t]*\[\][ \t]*"
    r"# managed by Bob Companion[ \t]*$"
)


def _write_codex_config_atomic(dest: Path, text: str) -> bool:
    """Replace one Codex config through a sibling file; never raises."""
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = dest.lstat()
        except FileNotFoundError:
            mode = None
        else:
            if not stat.S_ISREG(current.st_mode):
                logger.info("Codex config is not a regular file; leaving %s alone", dest)
                return False
            mode = stat.S_IMODE(current.st_mode)
        fd, name = tempfile.mkstemp(
            prefix=dest.name + ".dark-army.", suffix=".tmp", dir=dest.parent,
        )
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except OSError:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Could not write Codex terminal_title", exc_info=True)
        return False
    return True


def _set_codex_title_writer(path: Path | None = None) -> None:
    """Own the marked ``terminal_title = []`` line only when not user-owned."""
    dest = path if path is not None else CODEX_CONFIG_PATH
    try:
        if dest.is_symlink():
            logger.info("Codex config is a symlink; leaving %s alone", dest)
            return
        text = dest.read_text(encoding="utf-8") if dest.exists() else ""
    except (OSError, UnicodeError):
        logger.info("Could not read Codex config", exc_info=True)
        return
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError:
        logger.info("Codex config.toml is not parseable; not touching terminal_title")
        return
    if not isinstance(data, dict):
        return

    span = _table_span(text, _CODEX_TUI_TABLE)
    tui = data.get("tui")
    if tui is not None and not isinstance(tui, dict):
        logger.info("Codex tui setting is not a table; leaving %s alone", dest)
        return

    if span is None:
        if tui is not None:
            logger.info(
                "Codex tui is declared outside a [tui] header; leaving %s alone",
                dest,
            )
            return
        if text:
            new = text + "\n[tui]\n" + _CODEX_TITLE_MARKER + "\n"
        else:
            new = "[tui]\n" + _CODEX_TITLE_MARKER + "\n"
    else:
        _header_start, body_start, body_end = span
        body = text[body_start:body_end]
        marked = _CODEX_MARKED_TITLE.search(body)
        terminal_declared = isinstance(tui, dict) and "terminal_title" in tui
        if terminal_declared:
            if marked is None:
                logger.info(
                    "Codex terminal_title is user-owned; leaving %s alone", dest,
                )
            return
        body = _CODEX_TITLE_MARKER + "\n" + body
        new = text[:body_start] + body + text[body_end:]

    if new == text or not _write_codex_config_atomic(dest, new):
        return
    logger.info("Disabled Codex terminal_title in %s", dest)


def install_claude_hooks() -> None:
    """Merge Dark Army hooks into Claude Code settings without clobbering."""
    ok, settings = read_claude_settings(for_write=True)
    if not ok:
        # "Without clobbering" has to hold for the whole file, not just for the
        # hooks block we merge into.
        logger.warning("settings.json is not readable; not installing hooks")
        return
    settings["hooks"] = _merge_hook_config(settings.get("hooks"), HOOKS_CONFIG)
    write_claude_settings(settings)
    logger.info("Wrote the hook groups into %s", CLAUDE_SETTINGS_PATH)


def install_grok_rules() -> None:
    """Write ~/.grok/rules/dark-army.md. Grok loads home rules at session
    start; this is how it learns the bob-actions marker, because SessionStart
    stdout is ignored. Same write discipline as the hooks file: skipped when
    already current, replaced through a temp sibling + rename."""
    GROK_RULES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if GROK_RULES_PATH.read_text(encoding="utf-8") == GROK_RULES_TEXT:
            return
    except (OSError, UnicodeDecodeError):
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(GROK_RULES_DIR),
                                    prefix=GROK_RULES_PATH.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(GROK_RULES_TEXT)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, GROK_RULES_PATH)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Installed Grok rules in %s", GROK_RULES_PATH)


def are_grok_rules_installed() -> bool:
    try:
        return GROK_RULES_PATH.read_text(encoding="utf-8") == GROK_RULES_TEXT
    except (OSError, UnicodeDecodeError):
        return False


def install_grok_hooks() -> None:
    """Write ~/.grok/hooks/dark-army.json. Grok owns that directory; we
    replace only our file, never anyone else's in the same folder.

    Same discipline as the notify script: skipped when already current, and
    replaced through a temp sibling + rename rather than a truncating write
    Grok could read halfway through."""
    GROK_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"hooks": grok_hooks_config()}, indent=2) + "\n"
    try:
        if GROK_HOOKS_PATH.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeDecodeError):
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(GROK_HOOKS_DIR),
                                    prefix=GROK_HOOKS_PATH.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, GROK_HOOKS_PATH)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Installed hooks in %s", GROK_HOOKS_PATH)


def _backup_unusable_codex_hooks() -> None:
    """`_backup_unusable_settings`' rule for ~/.codex/hooks.json: a file that
    is not a JSON object is copied to a timestamped `.bak` once per distinct
    content before the key-merge replaces it. Best-effort, never fatal."""
    try:
        current = CODEX_HOOKS_PATH.read_bytes()
        for existing in CODEX_HOOKS_PATH.parent.glob(
                CODEX_HOOKS_PATH.name + ".*.bak"):
            try:
                if existing.read_bytes() == current:
                    return
            except OSError:
                continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = CODEX_HOOKS_PATH.with_name(
            f"{CODEX_HOOKS_PATH.name}.{stamp}.bak")
        shutil.copy2(CODEX_HOOKS_PATH, backup)
        logger.warning("Could not parse %s — it will be replaced. Previous "
                       "contents saved to %s", CODEX_HOOKS_PATH, backup)
    except OSError:
        logger.warning("Could not parse %s and it could not be backed up; "
                       "it will be replaced", CODEX_HOOKS_PATH, exc_info=True)


def install_codex_hooks() -> None:
    """Key-merge the shunt group into ~/.codex/hooks.json.

    A file the user wrote is read, merged through `_merge_hook_config` (only
    Dark Army's own groups are pruned and rewritten; every foreign event and group
    is kept) and written back through a temp sibling and `os.replace`;
    skipped when already current. A file that is not a JSON object is backed
    up and replaced — unlike Claude's settings.json this file holds hooks
    and nothing else, so there is no model, permission or statusLine to
    lose, and a hooks file Codex cannot parse is one it will not run anyway.
    Dark Army writing the file does not make Codex run it: hooks are trusted once
    in the TUI, and until then Codex is skill-and-brief only."""
    CODEX_HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CODEX_HOOKS_PATH.exists():
        try:
            loaded = json.loads(CODEX_HOOKS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
        else:
            _backup_unusable_codex_hooks()
    merged = dict(data)
    merged["hooks"] = _merge_hook_config(
        copy.deepcopy(data.get("hooks")) if isinstance(data.get("hooks"), dict)
        else {}, codex_hooks_config())
    text = json.dumps(merged, indent=2) + "\n"
    try:
        if CODEX_HOOKS_PATH.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeDecodeError):
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(CODEX_HOOKS_PATH.parent),
                                    prefix=CODEX_HOOKS_PATH.name + ".",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, CODEX_HOOKS_PATH)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Installed hooks in %s", CODEX_HOOKS_PATH)


def _best_effort(label: str, writer) -> None:
    """Run one of the Grok or Codex writers `install_hooks` calls after the
    Claude settings write. An unwritable `~/.grok` or `~/.codex`, or a
    `~/.codex` that is a regular file, is logged and the rest still
    happens: the menu bar must come up whatever the state of a harness the
    person may not even use. The Claude settings write is the one that must
    succeed and never goes through here."""
    try:
        writer()
    except OSError as exc:
        logger.warning("Could not install %s (%s); continuing without it",
                       label, exc)


def install_hooks() -> bool:
    """Install Dark Army hooks for Claude Code, Grok and Codex. Idempotent.
    The shunt guard rides the Claude and Grok files as a second PreToolUse
    group and is the only thing written for Codex.

    **The two scripts the groups name are written here first**, so no
    caller can register a hook whose file does not exist: a PreToolUse
    group pointing at a missing file exits 2 and refuses every Read and
    Bash call in every Claude session on the machine (the 20 Sep 2026
    lock-out). Both writes are skipped when the installed copy is current,
    so the order costs nothing on the ordinary launch. The Grok and Codex
    files are best-effort (`_best_effort`): an `OSError` there is a
    warning, never a failed launch."""
    install_notify_script()
    install_shunt_script()
    install_claude_hooks()
    _best_effort("Grok hooks", install_grok_hooks)
    _best_effort("Codex hooks", install_codex_hooks)
    _best_effort("Grok rules", install_grok_rules)
    return True
