"""Reveal a Claude Code session in VS Code: raise its window, focus its terminal tab.

Both halves derive from the session's Claude PID:

  - **Terminal tab** — the ``dark-army-ide`` VS Code extension. Each window runs
    its own loopback HTTP server and writes ``~/.dark-army/ide/<port>.lock``. The
    daemon fans a ``reveal_terminal`` request out to every window; the one that owns
    the terminal running the PID self-selects (ancestor-walk of ``Terminal.processId``,
    tty fallback) and calls ``terminal.show()``.
  - **Window raise** — from outside the process. A background app (and an extension
    inside it) cannot self-focus on macOS. ``open -a`` only activates the *app* and
    shows whichever window was already frontmost, so with two windows open it is a
    no-op whenever the wrong one is already active — the panel is a nonactivating
    ``NSPanel``, so that is the usual case.

The CLI names a window in two different ways, and they are not interchangeable:

  - ``code <identity>`` reuses a window **only when the path is what that window
    was opened as**. A folder window and a saved ``.code-workspace`` can be named
    that way. An *untitled* workspace cannot: internally it is
    ``Workspaces/<id>/workspace.json``, but the CLI treats a ``workspace.json``
    path as a *file*, so ``findWindowOnFile`` misses every workspace folder and
    silently falls back to the last-active window. That is why Jump worked with
    one window and did nothing with another VS Code already in front.
  - ``code -g <file-in-that-folder>:1`` goes through ``findWindowOnFile`` and
    then ``window.focus()``. A file inside the untitled workspace's folder finds
    that window even when another one is key. It also *opens* that file, so we
    prefer one the window already has in a tab and, if we had to open something
    (usually README.md), close it again after the terminal is refocused.

``_window_target`` therefore only returns a folder or a ``.code-workspace`` file
the window claimed as its identity. Untitled, unknown, or pre-0.1.2 replies
return None and the raiser uses ``-g`` on a file from ``workspaceFolders``
instead. ``open -a`` stays the last resort: imprecise, but it can never spawn
a window.

**The raisers go through ``open``, not the ``code`` CLI.** Both name the same
window to the same running VS Code, but ``code`` is Electron: the shell script
re-execs the app binary with ``cli.js``, and that child can *deadlock forever*
during module init depending on the lineage of the process that spawned it. It
does, reliably, for a daemon that was relaunched by a bare ``exec`` instead of
through LaunchServices (see ``app.py`` ``_restart_now``): every ``code`` the
daemon starts hangs in ``mach_msg`` and is still hanging hours later, while the
identical command from any other parent returns in about a second. That is a
whole-machine outage of Jump — both targeted raisers time out and the old code
fell through to ``open -a``, which shows whichever window was already frontmost,
so Jump appeared to work for the window you were already looking at and to do
nothing for every other one. ``open`` is LaunchServices and has no such child;
``code`` survives only as a fallback if ``open`` itself fails.

The ``code`` script spawns Electron ``cli.js`` and a timeout that only kills the
script leaves ``cli.js`` holding the single-instance pipe. ``run_code`` runs the
CLI in its own process group and kills the group on timeout; ``reap_stale_code_cli``
clears the escapees — any wedged VS Code CLI, not only ours, because whoever
started it, it is holding the pipe that Jump and the extension install need.

No private Anthropic protocol is touched. See ``docs/vscode-jump-to-session.md`` and
the 2026-07-27 spike: neither raiser disconnects the session's live IDE link, unlike
the Anthropic WebSocket that exploration first eyed.

The identity chain (``ps -E`` for the SSE port, ``~/.claude/ide/<port>.lock`` for the
workspace) is a *fallback* used only when the extension has not yet activated in the
target window; the extension fan-out is authoritative when present.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil

from .paths import STATE_DIR
from . import subprocess_env

logger = logging.getLogger("dark-army")

# Per-window extension lock files (mirrors ~/.claude/ide/ but for our extension).
_BOB_IDE_DIR = STATE_DIR / "ide"
# Anthropic's per-window lock files, keyed by the CLAUDE_CODE_SSE_PORT.
_CLAUDE_IDE_DIR = Path.home() / ".claude" / "ide"

_AUTH_HEADER = "x-bob-companion-authorization"

# Candidate `code` CLI locations, in preference order. The binary is usually NOT on
# PATH on macOS (the app bundle ships it but does not symlink it), so the bundle path
# comes first.
_CODE_CANDIDATES = (
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code",
)

_code_bin_cache: Optional[str] = None


def find_code_binary() -> Optional[str]:
    """Locate the VS Code CLI. App-bundle paths first, then PATH, then Insiders.

    Cached: the answer does not change within a daemon run, and each miss is a few
    stat()s. Returns None if VS Code's CLI cannot be found at all.
    """
    global _code_bin_cache
    if _code_bin_cache is not None:
        return _code_bin_cache or None
    for cand in _CODE_CANDIDATES:
        if os.path.exists(cand):
            _code_bin_cache = cand
            return cand
    for name in ("code", "code-insiders"):
        found = shutil.which(name)
        if found:
            _code_bin_cache = found
            return found
    _code_bin_cache = ""  # remember the miss
    return None


# Files `code -g` can open to name a window via findWindowOnFile. Prefer
# something already likely to exist at the workspace root so we do not walk.
_GOTO_NAMES = (
    "README.md", "README", "readme.md",
    ".gitignore",
    "package.json", "pyproject.toml", "Cargo.toml",
    "go.mod", "Makefile", "setup.py",
    "AGENTS.md", "CLAUDE.md",
)

# A wedged VS Code CLI, by either of the two processes the `code` script becomes:
# the `bash …/app/bin/code` wrapper and the `…/app/out/cli.js` Electron child.
_STALE_CLI_MARKS = ("/app/bin/code", "/app/out/cli.js")
# `code --wait` is a long-lived CLI *by design* — it is what git opens as an
# editor and it blocks until the tab is closed. Never reap one.
#
# Matched as an *argument*, not as a substring of the whole line. `-w` is the
# documented short form and the common spelling in `core.editor`, so a literal
# "--wait" check missed it entirely and this reaper SIGKILLed the editor of any
# `git commit` left open past two minutes, losing the message. Both spellings,
# and the combined short forms git users write (`-nw`, `-rw`).
_STALE_CLI_KEEP_ARGS = ("--wait", "-w")


def _mark_in_command_head(command: str) -> bool:
    """True when a stale-CLI mark names the executable or the script it runs.

    The mark must be the executable (argv[0]) or the first script argument —
    never a later argument. A substring-anywhere match aimed this reaper at
    `grep -r /app/out/cli.js /tmp` and at any editor with that file open.
    VS Code's own paths contain spaces ("Visual Studio Code.app"), so tokens
    cannot be counted; instead the mark must end an argument and nothing
    option-shaped may precede it — the wrapper (`bash …/app/bin/code <ws>`)
    and the Electron child (`…/MacOS/Code …/app/out/cli.js --flags`) both
    put the mark before any `-` token, while a grep's pattern sits after one.
    """
    for mark in _STALE_CLI_MARKS:
        idx = command.find(mark)
        while idx != -1:
            end = idx + len(mark)
            if end == len(command) or command[end].isspace():
                prefix = command[:idx].split()
                if not any(tok.startswith("-") for tok in prefix):
                    return True
            idx = command.find(mark, idx + 1)
    return False


def _keeps_cli_alive(line: str) -> bool:
    """True if this `ps` line is a deliberately blocking `code --wait`."""
    for token in line.split():
        if token in _STALE_CLI_KEEP_ARGS:
            return True
        # A single-dash cluster: `-nw`, `-rw`, `-gw`. Not `--foo`, and not a
        # path that merely happens to contain a w.
        if (len(token) > 1 and token[0] == "-" and token[1] != "-"
                and "w" in token[1:] and "/" not in token):
            return True
    return False
_STALE_CLI_MIN_AGE_S = 120.0


def _kill_pg(pid: int) -> None:
    """Kill a process group, then the pid itself. Best-effort."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _etime_seconds(raw: str) -> float:
    """Parse `ps -o etime=` ([[dd-]hh:]mm:ss) into seconds. 0 on garbage."""
    text = (raw or "").strip()
    if not text:
        return 0.0
    days = 0
    if "-" in text:
        day_s, text = text.split("-", 1)
        try:
            days = int(day_s)
        except ValueError:
            return 0.0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        hours, minutes, secs = nums
    elif len(nums) == 2:
        hours, minutes, secs = 0, nums[0], nums[1]
    elif len(nums) == 1:
        hours, minutes, secs = 0, 0, nums[0]
    else:
        return 0.0
    return float(days * 86400 + hours * 3600 + minutes * 60 + secs)


def _run_process(argv: list, timeout: float) -> Optional[subprocess.CompletedProcess]:
    """Run argv in its own session; kill the whole group if it overruns.

    The VS Code `code` script spawns Electron `cli.js`. `subprocess.run(timeout=)`
    only kills the script, so the child keeps the single-instance pipe and every
    later `code` (including the jump raiser) waits out its own timeout.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=subprocess_env.clean_env(),
        )
    except OSError:
        return None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_pg(proc.pid)
        try:
            proc.communicate(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None
    return subprocess.CompletedProcess(
        argv, proc.returncode,
        out.decode(errors="replace"), err.decode(errors="replace"),
    )


def run_code(*args: str, timeout: float = 5.0) -> Optional[subprocess.CompletedProcess]:
    """Run the VS Code CLI. None if it cannot be found, fails to spawn, or times out."""
    code = find_code_binary()
    if not code:
        return None
    return _run_process([code, *args], timeout)


def reap_stale_code_cli(min_age_s: float = _STALE_CLI_MIN_AGE_S) -> int:
    """Kill wedged VS Code CLI processes — any of them, not just our installs.

    Every one of these holds the single-instance pipe, and while one is held the
    next `code` blocks: jump cannot raise a window and `--install-extension`
    cannot finish (which is how the extension sat a version behind for days).
    The CLI's whole job is to hand a path to the running app and exit, so it is
    finished in about a second; anything past `min_age_s` is wedged whoever
    started it, and *not* reaping someone else's is how the pipe stays blocked.
    `code --wait` is the one long-lived-by-design form and is always spared.

    Returns how many pids were signalled.

    `ps` output is bytes on purpose. With `text=True` this decodes as ASCII under
    a daemon launched with no `LANG` (a login item), and one non-ASCII byte in any
    process's command line anywhere on the machine raised `UnicodeDecodeError`
    from here — a `ValueError`, so it flew straight past the caller's
    `except (OSError, TimeoutExpired)` and aborted the whole reveal.
    """
    try:
        raw = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,etime=,command="], timeout=2.0,
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return 0
    killed = 0
    for line in raw.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        if not _mark_in_command_head(parts[2]):
            continue
        if _keeps_cli_alive(parts[2]):
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid() or _etime_seconds(parts[1]) < min_age_s:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed += 1
        logger.warning("reaped stale VS Code CLI pid %d (was blocking jump/install)", pid)
    return killed


def _file_in_folders(folders: list) -> Optional[str]:
    """A real file inside one of the folders, for `code -g`. None if there is none."""
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in _GOTO_NAMES:
            cand = os.path.join(folder, name)
            if os.path.isfile(cand):
                return cand
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                        return entry.path
        except OSError:
            pass
        git_head = os.path.join(folder, ".git", "HEAD")
        if os.path.isfile(git_head):
            return git_head
    return None


# ---- process introspection (async; never block the loop) ----

async def _ps(*args: str) -> str:
    """Run `ps <args>` and return trimmed stdout ('' on error/timeout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        return out.decode(errors="replace").strip()
    except (asyncio.TimeoutError, OSError):
        return ""


async def _ps_field(field: str, pid: int) -> str:
    return await _ps("-o", f"{field}=", "-p", str(pid))


async def _ps_env(pid: int) -> dict:
    """Read a process's environment via `ps -E`. Returns {} on failure.

    `ps -E` prints argv followed by the environment as space-separated KEY=VALUE
    tokens. Values with spaces cannot be recovered this way, but the keys we need
    (CLAUDE_CODE_SSE_PORT, TERM_PROGRAM) have simple values, so tokenising on
    whitespace and keeping the first `=` split is sufficient.
    """
    raw = await _ps("-E", "-ww", "-p", str(pid), "-o", "command=")
    env: dict = {}
    for tok in raw.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k and k.isupper() and k not in env:
                env[k] = v
    return env


def _normalize_tty(t: str) -> str:
    return (t or "").strip().replace("/dev/", "")


# ---- lock files ----

def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_claude_lock(port: str) -> Optional[dict]:
    """Read Anthropic's ~/.claude/ide/<port>.lock (authToken, workspaceFolders)."""
    if not port:
        return None
    return _read_json(_CLAUDE_IDE_DIR / f"{port}.lock")


# A lock's token is interpolated straight into a request header, so it has to be
# header-safe: anything with a CR/LF in it would let whoever wrote the file append
# headers — or a second pipelined request — to a POST the daemon signs and sends.
# The character class is the whole point; the length bound is only a sanity cap, so
# it stays wide enough not to second-guess what the extension chose to write (it
# writes a randomUUID, but a short token is its business, not ours to reject).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")


def _valid_port(v) -> Optional[int]:
    """A lock's port as an int, or None if it is not a usable TCP port.

    `int()` is done *here* rather than at the call site because the call site builds
    the arguments to `asyncio.gather`, which is outside `return_exceptions=True` —
    a single truncated lock file used to abort the whole fan-out (and with it the
    window raise) for every session on the machine.
    """
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        port = int(v)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _lock_pid(lock: dict) -> int:
    """The lock's extension-host pid, or 0 where it carries none usable."""
    for key in ("extHostPid", "pid"):
        raw = lock.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        if raw > 0:
            return raw
    return 0


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. `0` — no pid recorded — is
    read as alive, so an older extension's lock is not refused for a field
    it never had; a permission error is a live process owned by somebody
    else, which is still a live process."""
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _bob_ext_locks() -> list:
    """Every live dark-army extension lock (one per VS Code window).

    Validating here is what keeps the fan-out total: a lock that cannot be used is
    dropped with a warning, and the windows that *are* reachable still answer.

    A lock whose extension host is dead is deleted, not just skipped. The
    extension unlinks its own lock in `deactivate()`, which never runs after a
    reboot, a force-quit or a crash — so `~/.dark-army/ide/` kept one lock
    per window that ever existed, and `spawn_agent` (addressed, `locks[0]`)
    happily posted a card to a port nobody listened on while the live window
    for the same folder sat one file over. The daemon owns the directory and a
    dead pid is the one thing that cannot be a window somebody is typing into.
    """
    out = []
    try:
        for f in _BOB_IDE_DIR.glob("*.lock"):
            d = _read_json(f)
            if not d:
                continue
            port = _valid_port(d.get("port"))
            token = d.get("authToken")
            if port is None or not isinstance(token, str) or not _TOKEN_RE.match(token):
                logger.warning("ignoring malformed extension lock %s", f.name)
                continue
            if not pid_alive(_lock_pid(d)):
                logger.info("removing stale extension lock %s (window is gone)", f.name)
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            out.append({**d, "port": port, "authToken": token})
    except OSError:
        pass
    return _one_bridge_per_window(out)


#: The extension writes its id into its lock; a lock with no id is an older
#: bridge.
DARK_ARMY_EXTENSION_ID = "dark-army.dark-army-ide"


def _one_bridge_per_window(locks: list) -> list:
    """One lock per extension host, so one window answers once.

    During the extension swap a window that has not reloaded runs both the
    old and the new bridge in the same extension host: two locks, two ports,
    and a fan-out that would type every keystroke into the terminal twice.
    The Dark Army bridge wins; failing that, the higher `extensionVersion`,
    then the later `startedAt`. A lock with no usable pid is its own window.
    Order is otherwise kept (`spawn_agent` addresses `locks[0]`)."""
    def rank(lock):
        return (lock.get("extensionId") == DARK_ARMY_EXTENSION_ID,
                _parse_ext_version(lock.get("extensionVersion")),
                lock.get("startedAt") if isinstance(lock.get("startedAt"), (int, float)) else 0)

    best: dict = {}
    for i, lock in enumerate(locks):
        key = _lock_pid(lock) or ("no-pid", i)
        if key not in best or rank(lock) > rank(best[key][1]):
            best[key] = (best.get(key, (i,))[0], lock)
    return [lock for _, lock in sorted(best.values(), key=lambda pair: pair[0])]


# ---- HTTP client for the extension (hand-rolled; no dependency) ----

async def _post_json(port: int, token: str, body: dict, timeout: float = 3.0, *, before_write=None) -> Optional[dict]:
    """POST JSON to a bob extension server on loopback. Returns parsed JSON or None.

    The extension sends `Connection: close`, so we read to EOF. Kept dependency-free
    (asyncio streams) to match the daemon's minimal-deps policy.
    """
    payload = json.dumps(body).encode()
    req = (
        f"POST / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"{_AUTH_HEADER}: {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + payload
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        if before_write is not None and not await before_write():
            return None
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw = await asyncio.wait_for(reader.read(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except (OSError, asyncio.TimeoutError):
            pass
    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200 " not in status_line:
        return None
    try:
        return json.loads(body_bytes.decode(errors="replace"))
    except ValueError:
        return None


async def _fanout_reveal_terminal(pid: int, tty: str) -> Optional[dict]:
    """Ask every window's extension to reveal the terminal owning `pid`.

    The PID runs in exactly one window, so at most one server answers matched:true.
    Fired concurrently; returns the winner's response (with workspaceFolders)."""
    locks = _bob_ext_locks()
    if not locks:
        return None
    body = {"op": "reveal_terminal", "pid": pid, "tty": tty}
    results = await asyncio.gather(
        *(_post_json(int(l["port"]), l["authToken"], body) for l in locks),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, dict) and r.get("matched"):
            return r
    return None


# ---- window raise ----

_VSCODE_APPS = ("Visual Studio Code", "Visual Studio Code - Insiders")


async def _open_in_vscode(path: str) -> bool:
    """`open -a "Visual Studio Code" <path>` — LaunchServices, no Electron child.

    The primary raiser. LaunchServices forwards the path to the already-running
    app, which resolves it exactly as `code <path>` does (the same
    `findWindowOnFile` reuse rules apply, so a path `_window_target` vetted still
    reuses its window rather than opening a new one) and activates it — but
    without spawning `cli.js`, the process that deadlocks forever when the daemon
    is its ancestor. `open` is a tiny LaunchServices client and returns in
    milliseconds regardless of who started us.
    """
    if not path:
        return False
    for app in _VSCODE_APPS:
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", app, path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
            if rc == 0:
                return True
        except (asyncio.TimeoutError, OSError):
            continue
    return False


async def _activate_vscode() -> bool:
    """Bring VS Code to the foreground WITHOUT opening or targeting any window.

    The fallback, not the primary raiser: `open -a` activates the app and shows
    whichever window macOS last considered frontmost, which is the right one only
    by luck once a second window exists (`terminal.show()` does not reorder windows).
    Its virtue is that it can never spawn one, so it is what every case
    `_window_target` cannot name safely degrades to."""
    for app in _VSCODE_APPS:
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", app,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=5.0)
            if rc == 0:
                return True
        except (asyncio.TimeoutError, OSError):
            continue
    return False


def _window_target(match: dict) -> Optional[str]:
    """The path `code <path>` will *reuse* the matched window for, or None.

    Only a folder window or a saved `.code-workspace` can be named this way.
    Untitled workspaces cannot: the CLI treats `workspace.json` as a file, so
    naming it focuses the last-active window instead of the one we matched.
    Returning None is always safe (the caller uses `-g` or `open -a`);
    returning the wrong path is not.
    """
    # An extension older than 0.1.2 omits these keys entirely, and without them a
    # folder window is indistinguishable from an untitled workspace holding one
    # folder. Absent identity is "cannot name it", not "assume a folder".
    # `None` (present, null) is a real answer and means a folder.
    if "workspaceUri" not in match:
        return None

    uri = (match.get("workspaceUri") or "").strip()
    fs_path = (match.get("workspaceFsPath") or "").strip()
    folders = [f for f in (match.get("workspaceFolders") or []) if isinstance(f, str)]

    if fs_path:  # a saved .code-workspace file — name it directly
        return fs_path if os.path.isfile(fs_path) else None

    if uri.startswith("untitled:"):
        # `code Workspaces/<id>/workspace.json` is a file open, not a reuse.
        return None

    if uri:
        # Some other scheme (a remote or virtual workspace). Nothing local to name.
        return None

    # No workspace file at all: a plain folder window, identified by its one folder.
    # More than one without a workspace file should not happen; if it does, fall back.
    if len(folders) == 1 and os.path.isdir(folders[0]):
        return folders[0]
    return None


def _open_editors(match: dict) -> list:
    """Absolute paths of file tabs the matched window already has open."""
    out = []
    for p in match.get("openEditors") or []:
        if isinstance(p, str) and os.path.isfile(p):
            out.append(os.path.normpath(p))
    return out


def _goto_target(match: dict) -> Optional[str]:
    """A file `code -g` can use to find the matched window via findWindowOnFile.

    Works for untitled workspaces (and for a folder window, as a fallback)
    because the test is "does this window contain the file", not "was this
    window opened as this path". Prefer a tab the window already has — `-g`
    on a closed README.md opens it and used to leave it there.
    """
    folders = [f for f in (match.get("workspaceFolders") or []) if isinstance(f, str)]
    already = _open_editors(match)
    for path in already:
        for folder in folders:
            if path == folder or path.startswith(folder.rstrip(os.sep) + os.sep):
                return path
    if already:
        return already[0]
    return _file_in_folders(folders)


async def _fanout_close_editor(path: str) -> None:
    """Ask every window to close `path` if it has that tab. Best-effort.

    Only the window we just `-g`'d will have the leftover; the others no-op.
    Pre-0.1.3 extensions return unknown-op and are ignored.
    """
    locks = _bob_ext_locks()
    if not locks or not path:
        return
    body = {"op": "close_editor", "path": path}
    await asyncio.gather(
        *(_post_json(int(l["port"]), l["authToken"], body) for l in locks),
        return_exceptions=True,
    )


async def frontmost_session_pids(pids) -> set:
    """Of `pids`, the ones running in the terminal the user is looking at.

    The rule this serves — *don't interrupt someone about the window already in
    front of them* — was written down long before anything answered it: the call
    was introduced in `98bfbe3` against a function that did not exist, and a bare
    `except Exception` at the call site swallowed the AttributeError on every
    evaluation, so the suppression set was empty for its entire life.

    There is no pure-Python route to the answer. The frontmost *application* is
    all this process can see, and suppressing on that would silence every session
    in every VS Code window whenever one of them came forward. So the question is
    asked of the extension, over the same per-window servers `reveal` already
    fans out to: each window knows whether it is focused (`window.state.focused`)
    and which terminal is on top of it, and answers only for the pids it owns.

    Fails to the **empty set** in every direction — no extension, an old one that
    does not know the op, a window that does not answer in time. An alert that
    fires when it could have been suppressed is a small cost; one that is
    silently withheld because a lock file was stale is a missed interruption,
    which is the failure this whole path exists to avoid.
    """
    wanted = [int(p) for p in pids if isinstance(p, int) and p > 1]
    if not wanted:
        return set()
    locks = _bob_ext_locks()
    if not locks:
        return set()
    body = {"op": "frontmost", "pids": wanted}
    results = await asyncio.gather(
        *(_post_json(int(l["port"]), l["authToken"], body, timeout=1.5)
          for l in locks),
        return_exceptions=True,
    )
    out = set()
    for r in results:
        # Pre-0.1.4 extensions answer `{"error": "unknown op: frontmost"}` with a
        # 200, which has neither key and contributes nothing — the same way the
        # `close_editor` fan-out tolerates them.
        if not isinstance(r, dict) or not r.get("focused"):
            continue
        for p in r.get("matched") or []:
            if isinstance(p, int) and p in wanted:
                out.add(p)
    return out


async def _run_code(*args: str, timeout: float = 5.0) -> bool:
    result = await asyncio.to_thread(run_code, *args, timeout=timeout)
    return bool(result and result.returncode == 0)


async def _raise_window(target: str) -> bool:
    """Focus the window already open on `target`. Never bare `open -a`.

    `open` first, `code` only if that fails: they name the same window to the
    same app, but the CLI can hang forever (module docstring) and `open` cannot.
    """
    return await _open_in_vscode(target) or await _run_code(target)


async def _goto_file(path: str) -> bool:
    """Focus the window whose workspace contains `path`.

    `open -a <app> <file>` goes through the same `findWindowOnFile` lookup as
    `code -g`; what it does not carry is the `:1`, and a line number was never
    the point here — the file is a handle for naming a window, not somewhere we
    want the caret. The CLI stays as the fallback for the same reason as above.
    """
    return await _open_in_vscode(path) or await _run_code("-g", f"{path}:1")


# ---- send_text (type into a terminal without raising the window) ----

# send_text is 0.1.6. An older lock answers `{error: 'unknown op'}` with HTTP 200,
# which must not read as a successful type-in.
SEND_TEXT_MIN_VERSION = (0, 1, 6)
_CAN_SEND_TTL = 5.0
_can_send_cache: dict = {}
_can_close_cache: dict = {}
_in_vscode_cache: dict = {}
#: Bound on `_in_vscode_cache`: pids come and go, and the entries are kept
#: for a process's life. Cleared whole when reached — a re-ask costs one `ps`.
_IN_VSCODE_CACHE_MAX = 256


def _parse_ext_version(raw) -> tuple:
    """`0.1.6` → (0, 1, 6). Anything unparseable sorts below every real version."""
    if not isinstance(raw, str) or not raw.strip():
        return (0,)
    parts = []
    for piece in raw.strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            return (0,)
    return tuple(parts) or (0,)


def _lock_can_send(lock: dict) -> bool:
    return _parse_ext_version(lock.get("extensionVersion")) >= SEND_TEXT_MIN_VERSION


def _send_capable_locks() -> list:
    return [l for l in _bob_ext_locks() if _lock_can_send(l)]


# ---- spawn_agent (open a terminal running an argv, in one named window) ----

# spawn_agent is 0.1.8. An older lock answers `{error: 'unknown op'}` with HTTP
# 200, which must not read as a session that started.
#
# **Why 0.1.8 and not 0.1.7, which is where the op landed.** Two different
# builds shipped as 0.1.7: the op was added, packaged and installed, and then
# `spawn_agent`'s folder check was fixed to resolve symlinks (`realOrSelf`) and
# the .vsix was rebuilt *in place*. `vscode_extension.ensure_installed()` skips
# when `have >= want`, so every machine carrying the first 0.1.7 keeps it
# forever — and this gate admits it, so a dispatch reaches a window that then
# refuses "cwd is not a folder of this window" for every symlink-reachable
# project, while the version says it should work. The rule a version gate
# cannot enforce for itself: content must never change under a fixed version.
SPAWN_MIN_VERSION = (0, 1, 8)


def _lock_can_spawn(lock: dict) -> bool:
    return _parse_ext_version(lock.get("extensionVersion")) >= SPAWN_MIN_VERSION


def _spawn_capable_locks() -> list:
    return [l for l in _bob_ext_locks() if _lock_can_spawn(l)]


def _lock_owns(lock: dict, root: str) -> bool:
    """Whether this window has `root` as one of its workspace folders.

    Exact, not containment: the caller already resolved the card's root through
    `workspace.py`, and a window that merely has an ancestor of it open is not
    the window the person meant.
    """
    folders = lock.get("workspaceFolders")
    if not isinstance(folders, list):
        return False
    target = os.path.realpath(root).rstrip(os.sep)
    for folder in folders:
        if not isinstance(folder, str):
            continue
        if os.path.realpath(folder).rstrip(os.sep) == target:
            return True
    return False


async def spawn_agent(root: str, argv: list, name: str, *,
                      env_extra: Optional[dict] = None) -> Optional[dict]:
    """Open a terminal in the window owning `root`, running `argv`. Reply or None.

    `env_extra` is merged into the `env` payload **after**
    `subprocess_env.unset_payload()`, and only its `str -> str` pairs: the
    payload's job is to *delete* py2app's variables from the new terminal, and
    a merge in the other order would let an added key resurrect one. It is how
    `dispatch.spawn` hands the new session its `origin` stamp. `None` (the
    default) reproduces this function's previous body exactly.

    **Addressed, not fanned out** — the one place this differs from `send_text`,
    and deliberately. `send_text` fans out because only the target window can
    say whether it owns a given pid; here the caller already knows the folder, so
    exactly one window may be asked. Fanning a spawn out would risk two windows
    each opening a terminal and two agents doing the same card.

    Returns the extension's `{spawned, terminalName, shellPid}` reply, or None
    when no 0.1.8+ window owns that folder. `shellPid` is the terminal's own
    process — the agent binary, spawned directly — and is the receipt
    `dispatch.spawn` hands the daemon to prove which session came out of this
    terminal; it arrives only from windows reloaded onto 0.1.10+ and is absent
    (or null, on a slow `processId`) otherwise, which is a fallback and never a
    refusal. None is a refusal the caller must report in
    words: VS Code does not reload an extension under a running window, so a
    machine that has been upgraded still has older windows open and "nothing
    happened" is indistinguishable from a broken button.
    """
    if not root or not argv:
        return None
    locks = [l for l in _spawn_capable_locks() if _lock_owns(l, root)]
    if not locks:
        return None
    # The deletions first, the additions second. `extension.ts` copies every
    # key of this object into `createTerminal({env})` verbatim — a JSON null
    # deletes, a string sets — so no extension version moves for a new key.
    env = dict(subprocess_env.unset_payload())
    for key, value in (env_extra or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            env[key] = value
    body = {
        "op": "spawn_agent",
        "cwd": root,
        "shellPath": str(argv[0]),
        # Every element a string, exactly as built. Nothing here joins them.
        "shellArgs": [str(a) for a in argv[1:]],
        "name": str(name or "agent"),
        # JSON-null values: the extension deletes these from the terminal
        # env so a session Dark Army started does not inherit py2app's PYTHONHOME.
        # Plus whatever `env_extra` added, merged above and never before.
        "env": env,
    }
    reply = await _post_json(int(locks[0]["port"]), locks[0]["authToken"], body,
                             timeout=8.0)
    return reply


def _session_in_vscode(pid: int) -> bool:
    """Is this process a VS Code integrated terminal (Claude or Grok)?

    `TERM_PROGRAM=vscode` is the common case. Claude Code also exports
    `CLAUDE_CODE_SSE_PORT` when the IDE bridge is up; either is enough.
    A session in Terminal.app / iTerm fails both and must not be marked
    reachable — send_text cannot type there, and a handled mark would
    silence the banner for nothing.
    """
    try:
        raw = subprocess.check_output(
            ["ps", "-E", "-ww", "-p", str(pid), "-o", "command="],
            timeout=2.0,
        ).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    env: dict = {}
    for tok in raw.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            if k and k.isupper() and k not in env:
                env[k] = v
    return bool(env.get("CLAUDE_CODE_SSE_PORT")) or env.get("TERM_PROGRAM") == "vscode"


def _process_started(pid: int) -> Optional[float]:
    """The process's start time — its identity against pid reuse — or None
    when it cannot be read. A syscall, never a subprocess."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def _in_vscode(pid: int) -> bool:
    """Cached `_session_in_vscode`.

    A yes is kept for the life of that process: `ps -E` reads the
    environment the process was *started* with, which cannot change, so
    re-asking every `_CAN_SEND_TTL` only re-forked `ps` — once a second
    across a fleet, on every push, the daemon's steadiest spawn. The start
    time (`_process_started`) keys it, so a reused pid asks again. A no, and
    any answer for a process whose start time cannot be read, keeps the old
    `_CAN_SEND_TTL` expiry: a failed `ps` also reads no, and must be retried.
    """
    now = time.monotonic()
    started = _process_started(pid)
    cached = _in_vscode_cache.get(pid)
    if cached is not None:
        ok, at, was_started = cached
        if was_started == started and (
                (ok and started is not None) or now - at < _CAN_SEND_TTL):
            return ok
    ok = _session_in_vscode(pid)
    if len(_in_vscode_cache) >= _IN_VSCODE_CACHE_MAX:
        _in_vscode_cache.clear()
    _in_vscode_cache[pid] = (ok, now, started)
    return ok


def can_send_text(pid, tty: str = "") -> bool:
    """Is there a 0.1.6+ window that could type into this process?

    Cheap on purpose: this is called from the snapshot thread for every full
    session. A lock whose version is new enough, plus a pid that looks like
    it is in VS Code, is enough to *try*; the send itself is what learns
    whether that window actually owns the terminal. Cached for the 5s a
    snapshot lives so a fleet of full sessions does not re-glob and re-`ps`
    once each. The `ps` itself is shared with `can_close_terminal` via
    `_in_vscode`.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    key = (pid, _normalize_tty(tty))
    now = time.monotonic()
    cached = _can_send_cache.get(key)
    if cached is not None:
        ok, at = cached
        if now - at < _CAN_SEND_TTL:
            return ok
    ok = bool(_send_capable_locks()) and _in_vscode(pid)
    _can_send_cache[key] = (ok, now)
    return ok


def _invalidate_can_send_cache() -> None:
    """Tests only — the cache is otherwise left to expire."""
    _can_send_cache.clear()
    _can_close_cache.clear()
    _in_vscode_cache.clear()


async def send_text(pid: int, tty: str, text: str,
                    newline: bool = True) -> Optional[dict]:
    """Type `text` into the VS Code terminal that owns `pid`. No focus change.

    Fans out only to locks whose extension is 0.1.6+. First `{matched: true}`
    wins. Returns that window's identity (for the log line) or None.
    """
    if not text:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 1:
        return None
    locks = _send_capable_locks()
    if not locks:
        return None
    if not tty:
        tty = _normalize_tty(await _ps_field("tty", pid))
    body = {
        "op": "send_text",
        "pid": pid,
        "tty": tty or "",
        "text": text,
        "newline": bool(newline),
    }
    results = await asyncio.gather(
        *(_post_json(int(l["port"]), l["authToken"], body) for l in locks),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, dict) and r.get("matched"):
            return r
    # Nothing typed anywhere. A window that owns the terminal but refused in
    # words — 0.1.21+ in a folder VS Code has not trusted — says so with an
    # explicit `sent: false` and an `error`; hand that sentence up (still
    # not sent, so every caller's `sent` test refuses) instead of letting it
    # read as "no window". An old window's `{error: "unknown op"}` carries no
    # `sent` and stays None.
    for r in results:
        if (isinstance(r, dict) and r.get("sent") is False
                and isinstance(r.get("error"), str) and r["error"].strip()):
            return {"matched": False, "sent": False, "error": r["error"].strip()}
    return None


# ---- close_terminal (dispose the terminal tab owning a pid) ----

# close_terminal is 0.1.9. An older lock answers `{error: 'unknown op'}` with
# HTTP 200, which must not read as a closed terminal — the caller falls back to
# the `/clear` route instead, so a stale window degrades to today's behaviour
# rather than to silence. Per the 0.1.8-not-0.1.7 lesson above: content never
# changes under a fixed version — any later change to this op bumps again.
CLOSE_TERMINAL_MIN_VERSION = (0, 1, 9)


def _lock_can_close(lock: dict) -> bool:
    return _parse_ext_version(lock.get("extensionVersion")) >= CLOSE_TERMINAL_MIN_VERSION


def _close_capable_locks() -> list:
    return [l for l in _bob_ext_locks() if _lock_can_close(l)]


def can_close_terminal(pid, tty: str = "") -> bool:
    """Is there a 0.1.9+ window that could dispose this process's tab?

    Sibling of `can_send_text`: same int/`pid <= 1` rejection, same composed
    cache, but the lock filter is `_close_capable_locks()` so a 0.1.6–0.1.8
    window that can type cannot draw a button whose press would land on
    `{error: 'unknown op'}`. The `ps` is `CLOSE_TERMINAL_MIN_VERSION`'s
    partner, not a second one — `_in_vscode` is shared with `can_send_text`.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    key = (pid, _normalize_tty(tty))
    now = time.monotonic()
    cached = _can_close_cache.get(key)
    if cached is not None:
        ok, at = cached
        if now - at < _CAN_SEND_TTL:
            return ok
    ok = bool(_close_capable_locks()) and _in_vscode(pid)
    _can_close_cache[key] = (ok, now)
    return ok


async def close_terminal(pid: int, tty: str) -> Optional[dict]:
    """Dispose the VS Code terminal tab that owns `pid`. Reply or None.

    Fans out exactly as `send_text` does and for its reason: only the window
    owning the pid answers `{matched: true}`, every other window is a no-op.
    Only 0.1.9+ locks are asked — the version gate is the fail-closed half.
    Returns the matching window's reply (for the log line) or None, which the
    caller must treat as "not closed" and fall back from.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 1:
        return None
    locks = _close_capable_locks()
    if not locks:
        return None
    if not tty:
        tty = _normalize_tty(await _ps_field("tty", pid))
    body = {
        "op": "close_terminal",
        "pid": pid,
        "tty": tty or "",
    }
    results = await asyncio.gather(
        *(_post_json(int(l["port"]), l["authToken"], body) for l in locks),
        return_exceptions=True,
    )
    for r in results:
        # `matched`, never HTTP success: an old-style error reply is a 200.
        if isinstance(r, dict) and r.get("matched"):
            return r
    return None


REFINEMENT_CLOSE_MIN_VERSION = (0, 1, 12)
# The strict ops' POST wait. It must outlast the extension's own deadline in
# `strictNativeTerminal` (5 s since 0.1.21), so a slow answer arrives as an
# answer and never as a timeout that may have acted; and it must stay under
# the daemon's `REFINEMENT_CLOSE_TIMEOUT` (8 s) around both callers.
NATIVE_TERMINAL_POST_TIMEOUT = 6.0
NATIVE_REPLY_MIN_VERSION = (0, 1, 21)


def native_reply_text(text) -> Optional[str]:
    """Only a bounded plain replacement input line; never terminal commands."""
    if (not isinstance(text, str) or len(text) > 2000
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in text)):
        return None
    text = text.strip()
    return text if text and text[0] not in "/!#" else None


def can_reply_native_terminal(pid: int, root: str) -> bool:
    """Cached native proof plus one compatible bridge; no foreign environment.

    The addressed bridge verifies terminal ancestry/tty before it may send.
    Legacy _in_vscode uses environment inspection and is not reply authority.
    """
    if type(pid) is not int or pid <= 1 or not root:
        return False
    locks = [lock for lock in _bob_ext_locks() if _lock_owns(lock, root)]
    return (len(locks) == 1
            and _parse_ext_version(locks[0].get("extensionVersion")) >= NATIVE_REPLY_MIN_VERSION)


async def reply_native_terminal(pid: int, tty: str, root: str, text: str, validate, *, expires_at_ms: int) -> Optional[dict]:
    """One addressed asyncio POST; cancellation before write cannot send later."""
    text = native_reply_text(text)
    if (text is None or type(pid) is not int or pid <= 1 or not _normalize_tty(tty) or not root
            or type(expires_at_ms) is not int or expires_at_ms <= time.time() * 1000):
        return None
    def prepare():
        locks = [lock for lock in _bob_ext_locks() if _lock_owns(lock, root)]
        if (len(locks) != 1
                or _parse_ext_version(locks[0].get("extensionVersion")) < NATIVE_REPLY_MIN_VERSION):
            return None
        return dict(locks[0])
    lock = await asyncio.get_running_loop().run_in_executor(None, prepare)
    if lock is None:
        return None
    return await _post_json(int(lock["port"]), lock["authToken"], {
        "op": "reply_native_terminal", "pid": pid, "tty": tty, "text": text,
        "expires_at_ms": expires_at_ms,
    }, timeout=NATIVE_TERMINAL_POST_TIMEOUT, before_write=validate)


def can_close_native_terminal(pid: int, root: str) -> bool:
    """Snapshot reach for strict disposal; process ancestry uses the shared cache.

    The click repeats exact journal ownership and the bridge's fresh ancestry
    and tty match. A compatible window alone never grants the capability.
    """
    if type(pid) is not int or pid <= 1 or not root:
        return False
    locks = [lock for lock in _bob_ext_locks() if _lock_owns(lock, root)]
    return (len(locks) == 1
            and _parse_ext_version(locks[0].get("extensionVersion")) >= REFINEMENT_CLOSE_MIN_VERSION
            and _in_vscode(pid))


async def close_refinement_terminal(pid: int, tty: str, root: str, validate) -> Optional[dict]:
    """One strict, addressed disposal with final ownership validation before POST.

    No fan-out or legacy retry. Preparation runs off-loop; validate consumes the
    caller's authority only when all preparatory awaits have finished. The
    human stopped-session caller uses the same strict transport without an
    attachment receipt; it must independently prove the turn has ended.
    """
    if type(pid) is not int or pid <= 1 or not _normalize_tty(tty) or not root:
        return None
    def prepare():
        locks = [lock for lock in _bob_ext_locks() if _lock_owns(lock, root)]
        if (len(locks) != 1
                or _parse_ext_version(locks[0].get("extensionVersion")) < REFINEMENT_CLOSE_MIN_VERSION
                or not _session_in_vscode(pid)):
            return None
        return dict(locks[0])
    lock = await asyncio.get_running_loop().run_in_executor(None, prepare)
    if lock is None:
        return None
    return await _post_json(int(lock["port"]), lock["authToken"], {
        "op": "close_refinement_terminal", "pid": pid, "tty": tty,
    }, timeout=NATIVE_TERMINAL_POST_TIMEOUT, before_write=validate)


# ---- orchestration ----

async def reveal(pid: int) -> tuple:
    """Raise the VS Code window for `pid` and focus its terminal tab. (ok, detail).

    Tab first, then window: the extension's reply is what tells us which window to
    raise, so the raise cannot come first. `-g` then steals editor focus, so the
    tab is shown again after a targeted raise. All I/O is async and best-effort.
    """
    env = await _ps_env(pid)
    sse_port = env.get("CLAUDE_CODE_SSE_PORT", "")
    term = env.get("TERM_PROGRAM", "")

    if not sse_port and term != "vscode":
        logger.info(
            "reveal: PID %d is not a VS Code session (TERM_PROGRAM=%r) — nothing to reveal",
            pid, term,
        )
        return False, "not a VS Code session"

    tty = _normalize_tty(await _ps_field("tty", pid))

    # A leaked `code --install-extension` holds the CLI pipe; without this, the
    # raise below times out and we fall through to `open -a` (wrong window).
    await asyncio.to_thread(reap_stale_code_cli)

    # Tab first: the owning window's extension focuses the terminal *and* reports
    # what that window was opened as, which is the only thing that can name it.
    match = await _fanout_reveal_terminal(pid, tty)

    target = _window_target(match) if match else None
    already = set(_open_editors(match)) if match else set()
    goto = None
    how = ""
    raised = False
    if target:
        raised = await _raise_window(target)
        how = f" via {os.path.basename(target)}" if raised else ""
    if not raised and match:
        # Untitled (and anything `_window_target` cannot name): -g a file the
        # window contains. That is what actually calls window.focus() when
        # another VS Code window is already key.
        goto = _goto_target(match)
        if goto:
            raised = await _goto_file(goto)
            how = f" via -g {os.path.basename(goto)}" if raised else ""

    named = target or goto
    if not raised and not named:
        # Only when nothing could be named. `open -a` shows whichever window was
        # already frontmost, so once we *do* know which window we want, falling
        # back to it is worse than doing nothing: it actively raises the wrong
        # window over the right one and then reports success. That fallthrough is
        # what made a machine-wide raise failure look like "Jump works for the
        # project I'm looking at and not for the others".
        raised = await _activate_vscode()

    if named and not raised:
        detail = (
            f"terminal tab focused, but the window would not come forward "
            f"(naming {os.path.basename(named)} failed)"
        )
        logger.warning("reveal: PID %d %s", pid, detail)
        return False, detail

    if match and named and raised:
        # `code -g` returns when the CLI has *sent* the focus, not when VS Code
        # has applied it. A too-quick re-show loses to the editor that -g opens.
        if goto and how.startswith(" via -g"):
            await asyncio.sleep(0.2)
            # -g opens the file. If that tab was not already there, take it
            # away so Jump does not accumulate a README (or whatever we used).
            if os.path.normpath(goto) not in already:
                await _fanout_close_editor(goto)
        rematch = await _fanout_reveal_terminal(pid, tty)
        if rematch:
            match = rematch

    if match:
        detail = (
            f"revealed (tab via {match.get('matchedBy')}, "
            f"window {'raised' + how if raised else 'not raised'}"
            f"{'' if named else ', app-level only'})"
        )
        logger.info("reveal: PID %d %s", pid, detail)
        return True, detail

    # No window claimed the terminal — the extension is not active in that window yet
    # (freshly installed / needs a reload). We still bring VS Code forward (never a
    # new window), it just may not be the exact window. `code` is deliberately NOT
    # used here: it is the only thing that can spawn an extra window.
    logger.info(
        "reveal: PID %d — VS Code raised, but no window claimed the terminal "
        "(reload that VS Code window so the extension activates)", pid,
    )
    return raised, "VS Code raised; terminal tab unavailable (reload that window)"


# ---- the reverse direction: a terminal, back to a session ----

async def session_for_terminal(
    shell_pid: int,
    tty: str,
    candidates,
    ps_field=_ps_field,
) -> Optional[str]:
    """Which live session is running in the terminal `shell_pid`/`tty` owns.

    The mirror of the extension's `findTerminal`, walked from the other end:
    there a window holds the terminals and is handed a session pid; here the
    daemon holds the sessions and is handed a terminal. Same two rungs in the
    same order — **ancestry** (a candidate whose ppid chain reaches the shell),
    then **tty equality** (which survives a session reparented away from its
    shell) — because a session that is genuinely a child of this terminal is a
    stronger answer than one that merely shares its device.

    `candidates` is `[(session_id, pid, last_event)]`; `ps_field` is injected so
    the rules are testable without a process table. Several matches on one rung
    are broken by the highest `last_event` — `terminal_title`'s
    one-writer-per-tty rule, most-recently-active wins, which is the same
    `/clear`-successor tie broken the same way.

    Returns None on no match, a nonsense shell pid, or no candidates. The caller
    answers an HTTP request with this, and selecting a row is *aim* rather than
    control: a miss opens the panel plainly and a wrong pick highlights the
    wrong row, so neither outcome needs a refusal.
    """
    if shell_pid < 2 or not candidates:
        return None
    want_tty = _normalize_tty(tty)
    ppid_cache: dict = {}

    async def _ppid(pid: int) -> int:
        if pid in ppid_cache:
            return ppid_cache[pid]
        try:
            value = int(((await ps_field("ppid", pid)) or "").strip())
        except (TypeError, ValueError):
            value = 0
        ppid_cache[pid] = value
        return value

    by_ancestry: list = []
    by_tty: list = []
    for session_id, pid, last_event in candidates:
        try:
            pid = int(pid or 0)
        except (TypeError, ValueError):
            continue
        if not session_id or pid < 2:
            continue
        stamp = float(last_event or 0.0)
        cur = pid
        hit = False
        for _ in range(12):
            if cur <= 1:
                break
            if cur == shell_pid:
                by_ancestry.append((stamp, session_id))
                hit = True
                break
            cur = await _ppid(cur)
        if hit or not want_tty:
            continue
        if _normalize_tty(await ps_field("tty", pid)) == want_tty:
            by_tty.append((stamp, session_id))

    for bucket in (by_ancestry, by_tty):
        if bucket:
            bucket.sort(key=lambda pair: pair[0], reverse=True)
            return bucket[0][1]
    return None
