"""Which process is the agent session: a walk up from our parent.

A hook runs a few processes below the harness that owns the session (a shell,
an `env`, a `python3`), and every later action on the session — the tab
title, a Stop — needs the harness's own pid. So the walk climbs the parent
chain asking `ps` for each ancestor's name and, only when the name does not
settle it, its command line. Claude Code and Grok are both recognised; the
shared Grok leader is not a session. `NOTIFY_SCRIPT` in
`dark_army_menubar/hooks.py` carries its own copy of the walk, since the
hook script imports nothing of ours: a change here is a change there.
"""

import os
import re
import subprocess
from typing import Optional

_CLAUDE_ARGV_RE = re.compile(r"(^|/)claude($|\s)")
_GROK_ARGV_RE = re.compile(r"(^|/)grok($|\s)")
# `grok agent leader …` is the shared daemon (`use_leader`), not a tab.
# `grok agent --leader stdio` is how Dark Army *connects* to it and must not match.
_GROK_LEADER_RE = re.compile(r"(?:^|/)grok(?:\s|$).*\bagent\s+leader\b")


def _ps(field: str, pid: int) -> str:
    """Run `ps -o <field>= -p <pid>` and return trimmed stdout. Empty on error.

    Decoded by hand rather than with `text=True`: under a daemon launched without
    `LANG` (a login item) that decodes as ASCII, and one accented character in a
    process's argv would raise `UnicodeDecodeError` — a `ValueError`, which walks
    straight past the except below and out of a function whose whole contract is
    "empty on error".
    """
    try:
        r = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, timeout=1.0,
        )
        return r.stdout.decode(errors="replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def looks_like_claude(comm: str, command: str) -> bool:
    """Is this process a Claude Code session? The rule, in one place.

    Two callers depend on it for different reasons: find_claude_pid() below walks
    ancestors looking for a match, and the daemon checks it before sending a
    signal — where the question is really "is the PID we recorded still the
    process we recorded it for", since PIDs get recycled and a stale one aimed at
    SIGTERM is someone else's process.
    """
    return comm == "claude" or bool(_CLAUDE_ARGV_RE.search(command))


def is_grok_leader(command: str) -> bool:
    """The shared ``grok agent leader`` process, never a session TUI.

    Hooks under ``use_leader`` run as its children. Matching it as a session
    pointed every Grok row at one headless pid: no tab title (tty is ``??``),
    and Stop would have SIGTERM'd every session at once.
    """
    return bool(command) and bool(_GROK_LEADER_RE.search(command))


def looks_like_grok(comm: str, command: str) -> bool:
    """Is this process a Grok session? Same identity rule as looks_like_claude.

    The leader is excluded even when ``comm == "grok"`` — but only when
    ``command`` is present. The comm-only fast path in ``find_session_pid``
    still has to fetch argv before accepting a grok match.
    """
    if is_grok_leader(command):
        return False
    return comm == "grok" or bool(_GROK_ARGV_RE.search(command))


def looks_like_session(comm: str, command: str, provider: Optional[str] = None) -> bool:
    """Identity check scoped to the session's provider when we know it.

    An unknown provider accepts either harness — leftover sessions from before
    we stamped `provider` still have to be stoppable, and a recycled PID that
    is neither claude nor grok is still refused.
    """
    if provider == "grok":
        return looks_like_grok(comm, command)
    if provider == "claude":
        return looks_like_claude(comm, command)
    return looks_like_claude(comm, command) or looks_like_grok(comm, command)


def find_claude_pid() -> int:
    """The nearest Claude Code ancestor's pid, else our parent's.

    An ancestor counts when its process name is `claude` (the native binary)
    or its command line runs a program called `claude` (a node-wrapped
    install), per `looks_like_claude`. A word merely containing "claude"
    somewhere in its arguments never counts: an editor with a file of that
    name open, or a grep for it, is not the session.
    """
    pid, _provider = find_session_pid(prefer="claude")
    return pid


def find_session_pid(prefer: Optional[str] = None) -> tuple[int, str]:
    """Walk ancestors for a claude or grok process.

    Returns ``(pid, provider)``. ``prefer`` is ``"grok"`` or ``"claude"`` and
    picks that harness when both appear in the tree; otherwise the first match
    wins. Falls back to ``os.getppid()`` with provider ``"claude"`` if nothing
    matched — the same fallback find_claude_pid has always had.
    """
    if prefer is None and os.environ.get("GROK_SESSION_ID"):
        prefer = "grok"
    start = os.getppid()
    pid = start
    found_claude: Optional[int] = None
    found_grok: Optional[int] = None
    skip_grok_ancestors = False
    while pid > 1:
        comm = _ps("comm", pid)
        command = ""
        if looks_like_claude(comm, ""):
            found_claude = pid
        elif looks_like_grok(comm, ""):
            # comm is `grok` for both the TUI and the leader. Fetch argv
            # before accepting — otherwise every hook under use_leader
            # stamps the headless leader pid on every session.
            command = _ps("command", pid)
            if is_grok_leader(command):
                # The leader is shared across every tab. Empirically it is
                # parented under one of those TUIs, so walking further
                # claims that other session's pid — TitleWriter then
                # writes one tab, and `/clear` dedup evicts the rest.
                # The roster has this session's TUI; stop here.
                if prefer == "grok":
                    break
                skip_grok_ancestors = True
            elif looks_like_grok(comm, command) and not skip_grok_ancestors:
                found_grok = pid
        else:
            # Deliberately lazy: `ps -o command=` is a second subprocess, and this
            # walk runs on every hook event. comm alone settles the native-binary
            # case, which is the common one for both harnesses.
            command = _ps("command", pid)
            if looks_like_claude("", command):
                found_claude = pid
            elif looks_like_grok("", command) and not skip_grok_ancestors:
                found_grok = pid
        if prefer == "grok" and found_grok is not None:
            return found_grok, "grok"
        if prefer == "claude" and found_claude is not None:
            return found_claude, "claude"
        if prefer is None and (found_claude is not None or found_grok is not None):
            if found_claude is not None:
                return found_claude, "claude"
            return found_grok, "grok"
        try:
            pid = int(_ps("ppid", pid))
        except ValueError:
            break
    if prefer == "grok" and found_grok is not None:
        return found_grok, "grok"
    if found_claude is not None:
        return found_claude, "claude"
    if found_grok is not None:
        return found_grok, "grok"
    return start, ("grok" if prefer == "grok" else "claude")


# The stamp as `ps -E` prints it: the value runs to the next `NAME=` token or
# the end of the line. A stage may hold a space ("security review"), so the
# value is not one whitespace-delimited word; `origin.parse` still bounds and
# validates whatever comes back here.
_ORIGIN_ENV_RE = re.compile(
    r"(?:^|\s)BOB_COMPANION_ORIGIN=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|\s*$)")


def process_environment(pid: int) -> str:
    """`ps -E -o command= -p <pid>`: the argv and environment of one process
    the daemon's user owns, as one line. Empty on any error, as `_ps` is."""
    if not pid or pid <= 0:
        return ""
    try:
        r = subprocess.run(
            ["ps", "-E", "-o", "command=", "-p", str(pid)],
            capture_output=True, timeout=1.0,
        )
        return r.stdout.decode(errors="replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def process_origin(pid: int, environment: Optional[str] = None) -> str:
    """The `BOB_COMPANION_ORIGIN` a process itself carries, or "".

    A Grok session's hooks do not run in the session's environment: they
    run under the shared `grok agent leader`, which inherits the environment
    of whichever terminal first started it — so every Grok hook message
    carries *that* terminal's stamp, and every Grok session since was
    attributed to one old card. The session's own process (the `grok` the
    dispatch spawned) does carry its own stamp, and this reads it there.
    `environment` is the seam for the tests; the daemon passes none.
    """
    text = environment if environment is not None else process_environment(pid)
    m = _ORIGIN_ENV_RE.search(text or "")
    return m.group(1).strip()[:200] if m else ""
