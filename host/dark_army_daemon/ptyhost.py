"""The pseudo-terminals Dark Army owns: a real pty per board-dispatched session.

`dispatch.spawn` asks a VS Code window to open a terminal; `dispatch.spawn_local`
asks this module instead. The difference is who the parent is. Here Dark Army
*is* the parent: it opens the pty, starts the assistant on the slave side
with `cwd` at the card's root, reads the master side on the daemon loop into
a bounded ring and into the emulator (`vtgrid.Screen`), and types back into
it through `write()`. Because Dark Army holds the child pid exactly, the bind is an
identity rather than the elimination the extension path needs, and
`owns(pid)` is the one question every typing and closing route asks first
(`session_io.py`).

**The grid stays in memory; the process that holds the master outlives the
menu bar.** A terminal buffer on disk is every secret an agent ever printed,
so nothing here goes near `paths._PRIVATE_FILES`. The broker process
(`pty_broker.py`) holds the fds; a Dark Army restart reconnects and restores
the screen from that process. Close terminal still SIGHUPs that child.
Quit of Dark Army leaves the broker up while anything is still live.

Hooks and the transcript were never the editor's doing — the CLI reads
`~/.claude/settings.json` whoever its parent is — so a session started here
carries the project key from the same walk up from `cwd`, and the enrolment
gate holds unchanged. There is no bypass for "Dark Army started it", on purpose.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import logging
import os
import pty
import secrets
import signal
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import psutil

from . import origin, subprocess_env
from .vtgrid import MAX_COLS, MAX_ROWS, MIN_COLS, MIN_ROWS, Screen

logger = logging.getLogger("dark-army.ptyhost")

#: Raw bytes kept per terminal. Memory only.
RING_BYTES = 256 * 1024
#: How many terminals the greeting is sized to carry, and how much a single
#: `Screen.snapshot()` may add on top of that terminal's ring.
HELLO_TERMINALS_HEADROOM = 16
HELLO_SCREEN_HEADROOM_BYTES = 1024 * 1024
#: The largest single line either end of the broker socket will read: the
#: hello's worst case, which the `start` and `reap` replies share. The
#: greeting and both replies carry each terminal's whole ring as base64 plus
#: its screen snapshot, so one line is routinely hundreds of kilobytes and
#: was 866,927 bytes for three terminals; asyncio's default is 64 KiB, and a
#: line over the limit is a `LimitOverrunError` that used to strand the
#: daemon on a half-open connection where every start timed out as "broker
#: rpc failed". Referenced as a module global at every call site so a test
#: can monkeypatch it.
BROKER_LINE_LIMIT = HELLO_TERMINALS_HEADROOM * (
    RING_BYTES * 4 // 3 + 4096 + HELLO_SCREEN_HEADROOM_BYTES)
#: Raw bytes per `data` frame the broker sends. Base64 of this plus framing
#: stays under asyncio's default 64 KiB, so a daemon that predates
#: `BROKER_LINE_LIMIT` still reads a new broker's output.
DATA_FRAME_BYTES = 32 * 1024
#: How long the daemon waits before its first reconnect attempt, and the
#: ceiling the delay doubles to.
RECONNECT_FIRST_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
#: What `_broker_connect` answers. `refused` means nobody is listening and
#: `attach()` may spawn a broker; `unreadable` means somebody *is* — never
#: spawn a second one over a live socket, whose terminals would be stranded.
CONNECT_CONNECTED = "connected"
CONNECT_REFUSED = "refused"
CONNECT_UNREADABLE = "unreadable"
#: How long one request to the broker may wait for its reply.
BROKER_RPC_TIMEOUT = 8.0
BROKER_TIMEOUT_DETAIL = "the terminal host did not answer in time"
BROKER_LOST_DETAIL = "the terminal host connection was lost"
#: How long SIGHUP gets before SIGKILL on `close()`.
CLOSE_GRACE_SECONDS = 2.0
#: How long an exited terminal's last screen is kept for the pane to draw.
EXITED_RETENTION_SECONDS = 120.0
#: The size a terminal opens at until the panel says otherwise.
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
#: What the child is told it is running in. The CLIs negotiate down from
#: this rather than assuming graphics or mouse reporting.
TERM = "xterm-256color"
#: How long a pid → terminal answer from the descent walk is trusted.
DESCENT_CACHE_SECONDS = 5.0
#: The module a broker runs as. Matched as the exact argv element after an
#: exact `-m`, never as a substring of a joined command line: `grep`, an
#: editor and this project's own tests all mention this name.
BROKER_MODULE = "dark_army_daemon.pty_broker"
#: The most brokers one sweep will ever signal, so a pathological machine
#: cannot turn a single attach into a hundred signals.
MAX_STRAY_SWEEP = 8


def stray_broker_pids(entries, sock_path: str, live) -> list:
    """`entries` is an iterable of `(pid, argv)`; `live` the pids that must
    never be signalled. Returns confirmed strays, at most `MAX_STRAY_SWEEP`.

    Pure: no psutil, no signals, no clock — the whole identification rule is
    table-testable. A `(pid, argv)` is a confirmed stray only when the argv
    runs `-m` and `BROKER_MODULE` as two adjacent exact elements *and*
    names this exact socket (by `realpath`, in either `--sock P` or
    `--sock=P` form) *and* the pid is a positive int outside `live`. An argv naming no socket
    never matches: an argument-less broker derived its own path inside its
    own process and this function cannot prove which path that was.
    """
    if not sock_path:
        return []
    want = os.path.realpath(sock_path)
    live_pids = set(live or ())
    found: list = []
    for entry in entries or ():
        try:
            pid, argv = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        if pid in live_pids:
            continue
        if not isinstance(argv, (list, tuple)):
            continue
        if not all(isinstance(a, str) for a in argv):
            # A non-string element would shift the adjacency test; refuse.
            continue
        argv = list(argv)
        module_run = any(argv[i] == "-m" and argv[i + 1] == BROKER_MODULE
                         for i in range(len(argv) - 1))
        if not module_run:
            continue
        named = False
        for i, arg in enumerate(argv):
            if arg == "--sock":
                if i + 1 < len(argv) and os.path.realpath(argv[i + 1]) == want:
                    named = True
                    break
            elif arg.startswith("--sock="):
                if os.path.realpath(arg[len("--sock="):]) == want:
                    named = True
                    break
        if named:
            found.append(pid)
    found.sort()
    return found[:MAX_STRAY_SWEEP]


@dataclass
class PtyTerminal:
    handle: str
    name: str
    root: str
    pid: int
    master: int
    proc: object
    screen: Screen
    cols: int
    rows: int
    ring: bytearray = field(default_factory=bytearray)
    started_at: float = field(default_factory=time.time)
    exited_at: Optional[float] = None
    returncode: Optional[int] = None
    session_id: str = ""
    bytes_read: int = 0
    #: Input the pty did not take at once, drained on the loop by
    #: `_on_writable`. Darwin's pty input queue is ~1 KB, so a long line
    #: routinely lands in two halves.
    pending: bytearray = field(default_factory=bytearray)
    reading: bool = False
    writing: bool = False
    #: Callables handed every chunk read from this terminal, in order, and
    #: `None` once when it exits — the panel's stream (`terminal_stream`)
    #: is one. Loop thread only.
    subscribers: list = field(default_factory=list)

    @property
    def exited(self) -> bool:
        return self.exited_at is not None


class PtyHost:
    """Every pty Dark Army has opened, keyed by an opaque handle."""

    def __init__(self, *, ring_bytes: int = RING_BYTES,
                 cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS,
                 port: Optional[int] = None, persist: bool = False,
                 sock_path: Optional[str] = None,
                 pid_path: Optional[str] = None,
                 hook_sock: Optional[str] = None):
        self.ring_bytes = max(1024, int(ring_bytes))
        self.default_cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
        self.default_rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        self.port = port
        #: The daemon's private hook socket. Set, a child is told this and
        #: not the port; unset keeps the port, the shape an older caller
        #: (and an older broker's own engine) still uses.
        self.hook_sock = hook_sock or ""
        self.persist = bool(persist)
        self.sock_path = sock_path or ""
        # A string, like `sock_path`: nothing here reaches `paths`, and the
        # broker is the one that writes its own pid file.
        self.pid_path = pid_path or ""
        self.on_data = None
        self.on_exit = None
        self._terms: dict[str, PtyTerminal] = {}
        self._by_pid: dict[int, str] = {}
        self._by_session: dict[str, str] = {}
        self._descent: dict[int, tuple[Optional[str], float]] = {}
        # The loop the readers and writers are registered on, taken at the
        # first `start()`. Held so `_detach` never needs
        # `get_running_loop()` — `reap()` used to be called from a worker
        # thread, where that raises, was swallowed, and left the master fd
        # closed under a live selector key.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader = None
        self._writer = None
        self._listen_task = None
        self._rpc_id = 0
        self._pending: dict = {}
        #: Set by `disconnect()` alone: a restart and a quit must not
        #: reconnect. `attach()` clears it.
        self._closing = False
        self._reconnect_task = None
        #: The pid the broker announced in its hello, 0 until one does.
        self._broker_pid = 0
        #: The stray sweep is one shot per host, set before it runs.
        self._swept = False
        #: The `hook_sock` the connected broker's hello named (`""` from a
        #: broker older than the private hook socket), and whether that has
        #: been said once already — a reconnect loop must not repeat it.
        self.broker_hook_sock = ""
        self._old_broker_warned = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, root: str, argv: list, name: str, *,
                    env: Optional[dict] = None, cols: Optional[int] = None,
                    rows: Optional[int] = None) -> tuple:
        """Open a pty and start `argv` on it with `cwd=root`.
        `(ok, detail, pid)` — `dispatch.spawn`'s triple."""
        if self.persist:
            return await self._broker_start(root, argv, name, env=env,
                                            cols=cols, rows=rows)
        if not argv:
            return False, "nothing to start", None
        if not root or not os.path.isdir(root):
            return False, "that project folder is not there any more", None
        cols = max(MIN_COLS, min(MAX_COLS, int(cols or self.default_cols)))
        rows = max(MIN_ROWS, min(MAX_ROWS, int(rows or self.default_rows)))
        child_env = subprocess_env.clean_env(env)
        # The strip takes every inherited session identity, the origin stamp
        # included — the broker's own environment carries whichever press
        # started the app. The one in `env` is the caller's deliberate stamp
        # for *this* child (`spawn_local`, the broker's allow-list), so it
        # goes back on after the strip and nothing else does.
        stamp = (env or {}).get(origin.ENV_VAR)
        if stamp:
            child_env[origin.ENV_VAR] = str(stamp)
        child_env["TERM"] = TERM
        child_env["COLUMNS"] = str(cols)
        child_env["LINES"] = str(rows)
        # The one address rule: a client dials exactly what its environment
        # names. The socket when this host knows it; the port only when it
        # does not, never both.
        if self.hook_sock:
            child_env["DARK_ARMY_HOOK_SOCKET"] = self.hook_sock
        elif self.port:
            child_env["BOB_COMPANION_PORT"] = str(self.port)
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            return False, f"could not open a terminal: {exc}", None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

        def _controlling_tty() -> None:
            # Runs in the child after setsid: make the slave (already dup'd
            # onto fd 0) the controlling terminal, so job control and
            # SIGHUP-on-close both work.
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *[str(a) for a in argv], cwd=root, env=child_env,
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, preexec_fn=_controlling_tty,
                close_fds=True)
        except (OSError, ValueError) as exc:
            os.close(master)
            os.close(slave)
            return False, f"could not start {os.path.basename(str(argv[0]))}: {exc}", None
        os.close(slave)
        os.set_blocking(master, False)
        handle = "pty-" + secrets.token_hex(6)
        term = PtyTerminal(
            handle=handle, name=str(name or "agent")[:80], root=str(root),
            pid=int(proc.pid), master=master, proc=proc,
            screen=Screen(cols, rows), cols=cols, rows=rows)
        self._terms[handle] = term
        self._by_pid[term.pid] = handle
        loop = asyncio.get_running_loop()
        self._loop = loop
        loop.add_reader(master, self._on_readable, term)
        term.reading = True
        asyncio.ensure_future(self._await_exit(term))
        logger.info("pty %s started pid %d in %s (%dx%d)", handle, term.pid,
                    root, cols, rows)
        return True, term.name, term.pid

    def _on_readable(self, term: PtyTerminal) -> None:
        try:
            data = os.read(term.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            # EIO: the slave side is gone, the child has exited.
            self._detach(term)
            self._mark_exited(term)
            return
        if not data:
            self._detach(term)
            self._mark_exited(term)
            return
        self._ingest(term, data)
        cb = self.on_data
        if cb is not None:
            try:
                cb(term.handle, data)
            except Exception:
                logger.debug("pty %s: on_data failed", term.handle, exc_info=True)

    def _ingest(self, term: PtyTerminal, data: bytes) -> None:
        """Every byte the terminal printed, whichever side read it: the
        counter, the ring, the emulator (deferred — parsed on the next read
        of the grid, never per chunk on the loop) and the subscribers."""
        term.bytes_read += len(data)
        term.ring += data
        if len(term.ring) > self.ring_bytes:
            del term.ring[:len(term.ring) - self.ring_bytes]
        try:
            term.screen.defer(data)
        except Exception:  # the emulator must never take the reader down
            logger.debug("pty %s: emulator refused a chunk", term.handle,
                         exc_info=True)
        for sub in list(term.subscribers):
            try:
                sub(data)
            except Exception:
                logger.debug("pty %s: subscriber failed", term.handle, exc_info=True)

    def subscribe(self, handle: str, callback) -> bool:
        """Hand `callback` every chunk this terminal prints from now on, and
        `None` once when it exits. False for a terminal that is not here."""
        term = self._terms.get(handle or "")
        if term is None:
            return False
        if callback not in term.subscribers:
            term.subscribers.append(callback)
        return True

    def unsubscribe(self, handle: str, callback) -> None:
        term = self._terms.get(handle or "")
        if term is None:
            return
        try:
            term.subscribers.remove(callback)
        except ValueError:
            pass

    def screen(self, handle: str) -> Optional[Screen]:
        """The terminal's grid, parsed up to the last byte read."""
        term = self._terms.get(handle or "")
        return term.screen if term is not None else None

    def _on_loop(self) -> bool:
        """Whether this thread is the loop's own. Selector edits from any
        other thread are not safe, so `reap()` defers to the loop."""
        loop = self._loop
        if loop is None:
            return True
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _detach(self, term: PtyTerminal) -> None:
        """Take the master fd off the loop — reader and writer both — so a
        close never leaves a selector key on a number the kernel will hand
        to the next file opened. Loop thread only."""
        loop = self._loop
        if loop is None:
            term.reading = term.writing = False
            return
        if term.reading:
            try:
                loop.remove_reader(term.master)
            except (RuntimeError, ValueError, OSError):
                pass
            term.reading = False
        if term.writing:
            try:
                loop.remove_writer(term.master)
            except (RuntimeError, ValueError, OSError):
                pass
            term.writing = False
        term.pending.clear()

    def _mark_exited(self, term: PtyTerminal) -> None:
        if term.exited_at is None:
            term.exited_at = time.time()
            self._by_pid.pop(term.pid, None)
            logger.info("pty %s: pid %d exited", term.handle, term.pid)
            for sub in list(term.subscribers):
                try:
                    sub(None)
                except Exception:
                    logger.debug("pty %s: subscriber failed on exit",
                                 term.handle, exc_info=True)
            term.subscribers.clear()
            cb = self.on_exit
            if cb is not None:
                try:
                    cb(term.handle, term.returncode)
                except Exception:
                    logger.debug("pty %s: on_exit failed", term.handle,
                                 exc_info=True)

    async def _await_exit(self, term: PtyTerminal) -> None:
        try:
            code = await term.proc.wait()
        except Exception:
            code = None
        term.returncode = code
        self._mark_exited(term)

    def ring_since(self, handle: str, since_bytes: int) -> tuple[bytes, int, bool]:
        """Bytes the panel has not yet fed a native emulator.

        `since_bytes` is the caller's last `bytes_read`. When that cursor
        has fallen off the ring, the whole ring is returned and the third
        value is True so the view resets rather than splicing a hole.
        """
        term = self._terms.get(handle or "")
        if term is None:
            return b"", 0, False
        try:
            since = int(since_bytes)
        except (TypeError, ValueError):
            since = 0
        total = int(term.bytes_read)
        ring = bytes(term.ring)
        start = total - len(ring)
        if since < start:
            return ring, total, True
        if since >= total:
            return b"", total, False
        return ring[since - start:], total, False

    def write(self, handle: str, text: str = "", data: Optional[bytes] = None) -> bool:
        """Type `text` (or raw `data`) at the terminal. False only when it is gone.

        Persist mode queues the write on the loop; the boolean is whether
        the terminal is still known, not whether the broker has taken the
        bytes yet (`drain` waits for that).

        What the pty does not take at once is **queued**, never dropped and
        never reported as the process having ended: Darwin's pty input queue
        is about 1 KB, so a long line lands in two halves and the second
        waits for the CLI to read the first. The remainder is drained on the
        loop by `_on_writable`; `drain()` is how a caller waits for it with
        a bound, and `pending()` how it asks what is still owed.
        """
        blob = data if data is not None else (text or "").encode("utf-8", "replace")
        if self.persist:
            term = self._terms.get(handle)
            if term is None or term.exited:
                return False
            self._broker_cast(
                "write", handle=handle,
                text=blob.decode("utf-8", "replace"),
                b64=base64.b64encode(blob).decode("ascii"))
            return True
        term = self._terms.get(handle)
        if term is None or term.exited:
            return False
        data = blob
        if not data:
            return True
        if term.pending:
            # Order matters on an input line: behind what is already owed.
            term.pending += data
            self._arm_writer(term)
            return True
        try:
            n = os.write(term.master, data)
        except BlockingIOError:
            n = 0
        except OSError:
            return False
        rest = data[max(0, n):]
        if rest:
            term.pending += rest
            self._arm_writer(term)
        return True

    def _arm_writer(self, term: PtyTerminal) -> None:
        loop = self._loop
        if loop is None or term.writing or term.exited:
            return
        try:
            loop.add_writer(term.master, self._on_writable, term)
        except (RuntimeError, ValueError, OSError):
            return
        term.writing = True

    def _on_writable(self, term: PtyTerminal) -> None:
        if not term.pending:
            self._disarm_writer(term)
            return
        try:
            n = os.write(term.master, bytes(term.pending))
        except BlockingIOError:
            return
        except OSError:
            term.pending.clear()
            self._disarm_writer(term)
            return
        del term.pending[:max(0, n)]
        if not term.pending:
            self._disarm_writer(term)

    def _disarm_writer(self, term: PtyTerminal) -> None:
        if not term.writing:
            return
        loop = self._loop
        if loop is not None:
            try:
                loop.remove_writer(term.master)
            except (RuntimeError, ValueError, OSError):
                pass
        term.writing = False

    def pending(self, handle: str) -> int:
        """Bytes typed at this terminal that the pty has not yet taken."""
        term = self._terms.get(handle)
        return len(term.pending) if term is not None else 0

    async def drain(self, handle: str, timeout: float) -> bool:
        """Wait up to `timeout` for everything owed to land. True when the
        queue is empty (or the terminal is gone — nothing more will land)."""
        if self.persist:
            reply = await self._rpc("drain", handle=handle, timeout=timeout)
            return bool(reply.get("ok"))
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            term = self._terms.get(handle)
            if term is None or term.exited or not term.pending:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)

    def resize(self, handle: str, cols: int, rows: int) -> bool:
        if self.persist:
            term = self._terms.get(handle)
            if term is None:
                return False
            cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
            rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
            if (cols, rows) == (term.cols, term.rows):
                # Nothing to say: the panel used to state its size on every
                # poll, and every statement was a line to the broker.
                return True
            term.cols, term.rows = cols, rows
            term.screen.resize(cols, rows)
            self._broker_cast("resize", handle=handle, cols=cols, rows=rows)
            return True
        term = self._terms.get(handle)
        if term is None:
            return False
        cols = max(MIN_COLS, min(MAX_COLS, int(cols)))
        rows = max(MIN_ROWS, min(MAX_ROWS, int(rows)))
        if (cols, rows) == (term.cols, term.rows):
            return True
        try:
            fcntl.ioctl(term.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            return False
        term.cols, term.rows = cols, rows
        term.screen.resize(cols, rows)
        return True

    async def close(self, handle: str, grace: float = CLOSE_GRACE_SECONDS) -> bool:
        """SIGHUP the process group, SIGKILL after `grace`, close the master
        and forget the terminal. True when there was one to close."""
        if self.persist:
            reply = await self._rpc("close", handle=handle)
            term = self._terms.pop(handle, None)
            if term is not None:
                self._by_pid.pop(term.pid, None)
                if term.session_id:
                    self._by_session.pop(term.session_id, None)
            return bool(reply.get("ok"))
        term = self._terms.pop(handle, None)
        if term is None:
            return False
        self._by_pid.pop(term.pid, None)
        if term.session_id:
            self._by_session.pop(term.session_id, None)
        self._detach(term)
        if not term.exited:
            self._signal_group(term.pid, signal.SIGHUP)
            try:
                await asyncio.wait_for(term.proc.wait(), grace)
            except asyncio.TimeoutError:
                self._signal_group(term.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(term.proc.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            self._mark_exited(term)
        try:
            os.close(term.master)
        except OSError:
            pass
        logger.info("pty %s closed (pid %d)", handle, term.pid)
        return True

    @staticmethod
    def _signal_group(pid: int, sig) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    async def close_all(self) -> None:
        if self.persist:
            await self._rpc("close_all")
            self._terms.clear()
            self._by_pid.clear()
            self._by_session.clear()
            return
        for handle in list(self._terms):
            await self.close(handle, grace=0.5)

    def reap(self, now: Optional[float] = None) -> None:
        """Forget exited terminals older than `EXITED_RETENTION_SECONDS` —
        their master fd included. Called from the snapshot path, on the
        loop; from any other thread it **defers** to the loop with
        `call_soon_threadsafe`, because the fd comes off the selector before
        it is closed and the selector is the loop thread's alone. A reap that
        closed the fd from a worker used to leave a dead key behind, and the
        next pty to be handed that fd number was never read."""
        now = time.time() if now is None else now
        if not self._on_loop():
            loop = self._loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self.reap, now)
                except RuntimeError:
                    pass
            return
        for handle, term in list(self._terms.items()):
            if term.exited and now - (term.exited_at or now) > EXITED_RETENTION_SECONDS:
                self._terms.pop(handle, None)
                if term.session_id:
                    self._by_session.pop(term.session_id, None)
                self._detach(term)
                try:
                    os.close(term.master)
                except OSError:
                    pass

    # ── lookups ──────────────────────────────────────────────────────────────

    def owns(self, pid) -> Optional[str]:
        """The handle whose live child `pid` is, or descends from. None for
        an exited terminal (its pid may already be somebody else's), for a
        pid that is not an int, and for anything not started here."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid <= 1 or not self._by_pid:
            return None
        direct = self._by_pid.get(pid)
        if direct is not None:
            return direct
        now = time.monotonic()
        cached = self._descent.get(pid)
        if cached is not None and now - cached[1] < DESCENT_CACHE_SECONDS:
            return cached[0]
        found = None
        try:
            for parent in psutil.Process(pid).parents():
                handle = self._by_pid.get(parent.pid)
                if handle is not None:
                    found = handle
                    break
        except (psutil.Error, OSError):
            found = None
        self._descent[pid] = (found, now)
        if len(self._descent) > 512:
            self._descent = {p: v for p, v in self._descent.items()
                             if now - v[1] < DESCENT_CACHE_SECONDS}
        return found

    def bind(self, handle: str, session_id: str) -> bool:
        """Name the terminal after its session. **Once**: a terminal already
        named for another session is refused rather than re-pointed, because
        a background agent the dispatched session spawned descends from the
        same child pid and would otherwise steal the pane every other frame."""
        term = self._terms.get(handle)
        if term is None or not session_id:
            return False
        if term.session_id and term.session_id != session_id:
            return False
        term.session_id = session_id
        self._by_session[session_id] = handle
        if self.persist:
            self._broker_cast("bind", handle=handle, session_id=session_id)
        return True

    def unbind(self, session_id: str) -> None:
        """Forget the name a terminal wears, leaving the terminal running.
        The one way a named terminal is ever re-pointed: `/clear` ends the
        session and starts a fresh id **in the same process**, so the pane
        would otherwise stay named for a session Dark Army has forgotten and the
        successor could never be resolved. Called from `_forget_session`
        alone — while Dark Army still holds the session, `bind` keeps refusing.
        A session with no terminal is a no-op, and so is an **exited** one:
        a dead child gets no successor, and the name is what lets the
        finished row keep its last screen and its Close terminal for
        `EXITED_RETENTION_SECONDS` — `reap` pops it when the window ends."""
        handle = self._by_session.get(session_id or "")
        if handle is None:
            return
        term = self._terms.get(handle)
        if term is not None and term.exited:
            return
        if self.persist:
            self._broker_cast("unbind", session_id=session_id)
        self._by_session.pop(session_id, None)
        if term is not None and term.session_id == session_id:
            term.session_id = ""

    def for_session(self, session_id: str) -> Optional[str]:
        handle = self._by_session.get(session_id or "")
        if handle is not None and handle not in self._terms:
            self._by_session.pop(session_id, None)
            return None
        return handle

    def get(self, handle: str) -> Optional[PtyTerminal]:
        return self._terms.get(handle or "")

    def terminals(self) -> list:
        return list(self._terms.values())

    @property
    def connected(self) -> bool:
        """Whether `_terms` is the broker's word right now. Off the persistent
        path it always is (the terminals are this process's own); on it, only
        while the link to the broker is up — between a drop and the
        reconnect the map is empty and says nothing about the terminals."""
        if not self.persist:
            return True
        return self._writer is not None and not self._writer.is_closing()

    def __len__(self) -> int:
        return len(self._terms)

    # ── persist: the broker that outlives the daemon ─────────────────────────

    async def attach(self) -> bool:
        """Connect to a live broker, or start one, and restore its terminals
        into this host. No-op when `persist` is off."""
        if not self.persist:
            return True
        self._closing = False
        if self._writer is not None and not self._writer.is_closing():
            return True
        if not self.sock_path:
            logger.warning("pty broker not started: no socket path was given")
            return False
        outcome = await self._broker_connect()
        connected = outcome == CONNECT_CONNECTED
        if not connected:
            if outcome == CONNECT_UNREADABLE:
                # Somebody is listening on that socket. Spawning a second
                # broker would unlink the live one's path and strand its
                # terminals for good; the reconnect loop retries instead.
                self._schedule_reconnect()
                return False
            self._spawn_broker()
            for _ in range(40):
                await asyncio.sleep(0.05)
                if await self._broker_connect() == CONNECT_CONNECTED:
                    connected = True
                    break
        if not connected:
            logger.warning("pty broker did not come up")
            return False
        # One sweep per host, on whichever leg above succeeded. The spawn leg
        # is the principal case — a stranded broker no longer listens, so the
        # first connect is refused and a fresh broker is started — and by here
        # `_broker_connect()` has taken that broker's pid out of its hello, so
        # the live exclusion is intact either way.
        if not self._swept:
            self._swept = True          # set first: a failed sweep never retries
            try:
                await asyncio.to_thread(self._sweep_strays)
            except Exception:
                logger.warning("pty broker sweep failed", exc_info=True)
        return True

    async def disconnect(self) -> None:
        """Drop the broker connection without closing any child. Restart
        and quit both take this path so the agents keep their terminals."""
        if not self.persist:
            return
        self._closing = True
        reconnect = self._reconnect_task
        self._reconnect_task = None
        if reconnect is not None:
            reconnect.cancel()
            # Awaited, not merely cancelled: a task still pending when the
            # loop closes is a warning on every test that disconnects.
            if reconnect is not asyncio.current_task():
                try:
                    await reconnect
                except (asyncio.CancelledError, Exception):
                    pass
        task = self._listen_task
        self._listen_task = None
        if task is not None:
            task.cancel()
        writer = self._writer
        self._writer = self._reader = None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def stop_broker(self) -> None:
        """Tell the broker to SIGHUP everyone and exit. Not the restart path."""
        if not self.persist:
            return
        try:
            await self._rpc("shutdown")
        except Exception:
            pass
        await self.disconnect()

    async def _broker_start(self, root, argv, name, *, env=None,
                            cols=None, rows=None):
        """The persistent path's `start`.

        `env` rides the RPC as a bounded `str -> str` object; the broker keeps
        only the keys `origin.ENV_KEY_RE` names, so this is an attribution
        channel and not a general-purpose environment injector. A broker
        already running from a build that predates the field ignores it — the
        session is then simply unattributed until that broker exits, which is
        degraded and never wrong.
        """
        if not await self.attach():
            return False, "Dark Army has no terminal host running", None
        env_payload = {
            str(k): str(v) for k, v in (env or {}).items()
            if isinstance(k, str) and isinstance(v, str)
            and origin.ENV_KEY_RE.match(k)
        }
        reply = await self._rpc("start", root=root, argv=list(argv),
                                name=name, env=env_payload or None,
                                cols=cols, rows=rows)
        if not reply.get("ok"):
            return False, str(reply.get("detail") or "could not start"), None
        desc = reply.get("terminal") or {}
        handle = str(reply.get("handle") or desc.get("handle") or "")
        pid = reply.get("pid")
        if handle:
            self._adopt(desc if desc else {
                "handle": handle, "pid": pid, "name": name, "root": root,
                "cols": cols or self.default_cols,
                "rows": rows or self.default_rows})
        return True, str(reply.get("detail") or name), pid

    def _adopt(self, desc: dict) -> None:
        handle = str(desc.get("handle") or "")
        if not handle:
            return
        try:
            pid = int(desc.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        cols = int(desc.get("cols") or self.default_cols)
        rows = int(desc.get("rows") or self.default_rows)
        screen = Screen(cols, rows)
        snap = desc.get("screen")
        if isinstance(snap, dict):
            screen.restore(snap)
        term = PtyTerminal(
            handle=handle, name=str(desc.get("name") or "agent")[:80],
            root=str(desc.get("root") or ""), pid=pid, master=-1, proc=None,
            screen=screen, cols=cols, rows=rows)
        try:
            term.bytes_read = int(desc.get("bytes_read") or 0)
        except (TypeError, ValueError):
            pass
        ring_b64 = desc.get("ring_b64")
        if isinstance(ring_b64, str) and ring_b64:
            try:
                term.ring = bytearray(base64.b64decode(ring_b64))
            except (ValueError, TypeError):
                pass
        if desc.get("exited"):
            term.exited_at = time.time()
            term.returncode = desc.get("returncode")
        self._terms[handle] = term
        if pid and not term.exited:
            self._by_pid[pid] = handle
        sid = str(desc.get("session_id") or "")
        if sid:
            term.session_id = sid
            self._by_session[sid] = handle

    async def _broker_connect(self) -> str:
        """`connected` / `refused` / `unreadable`. Only `refused` — nothing
        listening at all — lets `attach()` spawn a broker."""
        path = self.sock_path
        if not path or not os.path.exists(path):
            return CONNECT_REFUSED
        try:
            reader, writer = await asyncio.open_unix_connection(
                path, limit=BROKER_LINE_LIMIT)
        except OSError:
            return CONNECT_REFUSED
        # Nothing is kept until the greeting has been read whole: a
        # connection whose hello could not be read is closed and forgotten,
        # so `attach()` tries again rather than believing a link that has
        # no listener behind it.
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError, OSError) as exc:
            logger.warning(
                "pty broker hello exceeded %d bytes; not spawning a second "
                "broker (%s)", BROKER_LINE_LIMIT, exc)
            self._close_writer(writer)
            return CONNECT_UNREADABLE
        if not line:
            # Somebody accepted and hung up: a broker, just not a well one.
            self._close_writer(writer)
            return CONNECT_UNREADABLE
        try:
            hello = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            hello = {}
        try:
            self._broker_pid = int(hello.get("pid") or 0)
        except (TypeError, ValueError):
            self._broker_pid = 0
        #: What the broker says it tells its terminals. Empty from a broker
        #: older than the private hook socket: those terminals are on the
        #: port for their whole life, and the broker outlives the app on
        #: purpose (`docs/pty-broker-contract.md`), so it is said, not fixed.
        self.broker_hook_sock = str(hello.get("hook_sock") or "") \
            if isinstance(hello, dict) else ""
        if (self.hook_sock and not self.broker_hook_sock
                and not self._old_broker_warned):
            self._old_broker_warned = True
            logger.warning(
                "the pty broker predates the private hook socket; terminals "
                "it opens report over the network port until it is replaced")
        # The old terminals are kept just long enough to carry the panel's
        # open sockets across the reattach.
        old_terms = dict(self._terms)
        self._reader, self._writer = reader, writer
        self._loop = asyncio.get_running_loop()
        self._terms.clear()
        self._by_pid.clear()
        self._by_session.clear()
        for desc in hello.get("terminals") or []:
            if isinstance(desc, dict):
                self._adopt(desc)
        self._listen_task = asyncio.create_task(self._broker_listen())
        self._resume_subscribers(old_terms)
        logger.info("connected to pty broker (%d terminal(s))", len(self._terms))
        return CONNECT_CONNECTED

    @staticmethod
    def _close_writer(writer) -> None:
        try:
            writer.close()
        except Exception:
            pass

    def _resume_subscribers(self, old_terms: dict) -> None:
        """A reattach keeps the pane fed: every subscriber on the terminal
        this handle had before the link dropped moves onto the fresh
        `PtyTerminal`, and the bytes printed in the gap go out to them."""
        for handle, old in old_terms.items():
            term = self._terms.get(handle)
            if term is None or not old.subscribers:
                continue
            term.subscribers = list(old.subscribers)
            if term.bytes_read <= old.bytes_read:
                continue
            gap, _total, overflowed = self.ring_since(handle, old.bytes_read)
            if overflowed:
                logger.debug("pty %s: the gap across the reconnect fell off "
                             "the ring", handle)
                continue
            for sub in list(term.subscribers):
                try:
                    sub(gap)
                except Exception:
                    logger.debug("pty %s: subscriber failed on reattach",
                                 handle, exc_info=True)

    def _live_broker_pids(self) -> set:
        """Every pid the sweep must never signal. Over-excluding is safe;
        under-excluding kills somebody's terminals."""
        live = {os.getpid()}
        if self._broker_pid > 0:
            live.add(self._broker_pid)
        if self.pid_path:
            # `Path.read_text`, never the builtin:
            # `test_nothing_here_reaches_a_file_under_paths` greps this
            # module for that call form, and a pid file is not a transcript.
            try:
                recorded = int(Path(self.pid_path).read_text().strip())
            except (OSError, ValueError):
                recorded = 0
            if recorded > 0:
                live.add(recorded)
        return live

    def _signal_stray(self, pid: int) -> bool:
        """One termination signal at one broker pid — never a process group
        and never the uncatchable one, so `_signal_group` is deliberately not
        reused here. The stray's pty children get their SIGHUP from the
        master fds closing when it dies, which is what its own `close_all()`
        would have done. Its own method so a test can replace it rather than
        patch the global `os`.

        The argv is proved again here, immediately before the signal: the
        scan that named this pid ran earlier, and a stray that exited in
        between frees its number for anything on the machine to inherit.
        Returns whether the signal was actually sent, so the caller never
        claims a kill that did not happen."""
        try:
            argv = list(psutil.Process(pid).cmdline() or [])
        except psutil.Error:
            logger.info("stranded pty broker %d: gone before the signal", pid)
            return False
        if stray_broker_pids([(pid, argv)], self.sock_path,
                             self._live_broker_pids()) != [pid]:
            logger.info("stranded pty broker %d: no longer that process, "
                        "left alone", pid)
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.info("stranded pty broker %d not signalled: %s", pid, exc)
            return False
        logger.info("asked stranded pty broker %d to stop", pid)
        return True

    def _sweep_strays(self) -> list:
        """The one-shot backstop for a broker too wedged to retire itself.
        Sync, intended for a worker thread: `psutil.process_iter` is a
        whole-machine walk.

        The enabling condition is the hello pid and nothing else: the broker
        this daemon is connected to is the only one it positively
        identifies. A pid file is an **exclusion**, never a licence to
        sweep — a stale `pty.pid` from a previous boot, naming a number the
        machine has since recycled, would otherwise turn a broker built
        before the hello carried a pid into a target. Such a broker cannot
        retire itself either; the sweep starts working after one upgrade
        cycle, which is the right cost."""
        if self._broker_pid <= 0:
            logger.debug("pty broker sweep skipped: no live broker named")
            return []
        live = self._live_broker_pids()
        entries = []
        me = os.getuid()
        for proc in psutil.process_iter(["pid", "cmdline", "uids"]):
            try:
                info = proc.info
                uids = info.get("uids")
                # An unreadable uid field is not ownership: psutil reports
                # `None` where the platform refused, and over-excluding is
                # the safe direction for something that ends in a signal.
                if getattr(uids, "real", None) != me:
                    continue
                entries.append((info.get("pid"), info.get("cmdline") or []))
            except psutil.Error:
                continue
        pids = stray_broker_pids(entries, self.sock_path, live)
        # The WARNING names what was actually signalled, never the candidate
        # list: a stray that had already gone, or that this user may not
        # signal, is an INFO line inside `_signal_stray` and no kill.
        sent = [pid for pid in pids if self._signal_stray(pid)]
        if sent:
            logger.warning("asked %d stranded pty broker(s) to stop: %s",
                           len(sent), sent)
        return pids

    def _spawn_broker(self) -> None:
        env = os.environ.copy()
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cur = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = pkg_root + ((os.pathsep + cur) if cur else "")
        argv = [sys.executable, "-m", BROKER_MODULE]
        if self.port:
            argv.extend(["--port", str(self.port)])
        if self.hook_sock:
            argv.extend(["--hook-sock", self.hook_sock])
        # Both paths are always stated. `paths._home()` is per-pid under
        # pytest, so a broker left to derive its own would write its pid
        # file into a folder nobody can find — and an argument-less broker
        # is the one that outlives every sweep.
        argv.extend(["--sock", self.sock_path])
        argv.extend(["--pidfile", self.pid_path or os.path.join(
            os.path.dirname(self.sock_path) or ".", "pty.pid")])
        try:
            subprocess.Popen(
                argv, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                close_fds=True)
        except OSError:
            logger.warning("could not start pty broker", exc_info=True)

    async def _broker_listen(self) -> None:
        reader = self._reader
        writer = self._writer
        if reader is None:
            return
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                self._broker_ingest(msg)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        except (ValueError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError) as exc:
            partial = getattr(exc, "partial", b"") or b""
            logger.warning(
                "pty broker sent a line over %d bytes; reconnecting (%d byte(s)"
                " read)", BROKER_LINE_LIMIT, len(partial))
        finally:
            if self._writer is writer:
                self._writer = self._reader = None
            # A half-open socket left behind is a fd the broker still sees a
            # client on, so its idle rule would never fire.
            if writer is not None:
                self._close_writer(writer)
            # A request in flight when the link goes gets its answer now,
            # in words, rather than an eight-second timeout with none.
            pending = list(self._pending.values())
            self._pending.clear()
            for fut in pending:
                if not fut.done():
                    fut.set_result({"ok": False, "detail": BROKER_LOST_DETAIL})
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """One reconnect task at a time, and none once `disconnect()` has
        said the daemon is going down."""
        if self._closing or not self.persist:
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """A dropped link comes back on its own, without a daemon timer."""
        delay = RECONNECT_FIRST_SECONDS
        try:
            while not self._closing:
                await asyncio.sleep(delay)
                if self._closing:
                    return
                if self._writer is not None and not self._writer.is_closing():
                    return
                try:
                    if await self.attach():
                        return
                except Exception:
                    logger.debug("pty broker reconnect failed", exc_info=True)
                delay = min(RECONNECT_MAX_SECONDS, delay * 2)
        finally:
            self._reconnect_task = None

    def _broker_ingest(self, msg: dict) -> None:
        req_id = msg.get("id")
        if req_id is not None and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(msg)
            return
        event = msg.get("event")
        handle = str(msg.get("handle") or "")
        term = self._terms.get(handle)
        if event == "data" and term is not None:
            # Bytes stay bytes: `b64` is what today's broker sends, so a
            # multi-byte character split across two reads is whole again
            # here and the two sides' counters agree. The `data` string is
            # an older broker still running from before a restart.
            b64 = msg.get("b64")
            data = None
            if isinstance(b64, str) and b64:
                try:
                    data = base64.b64decode(b64)
                except (ValueError, TypeError):
                    data = None
            if data is None:
                data = str(msg.get("data") or "").encode("utf-8", "replace")
            self._ingest(term, data)
        elif event == "exit" and term is not None:
            term.returncode = msg.get("returncode")
            self._mark_exited(term)

    def _broker_cast(self, op: str, **fields) -> None:
        writer = self._writer
        loop = self._loop
        if writer is None or loop is None:
            return
        payload = {"op": op, **fields}
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

        def _write():
            try:
                writer.write(raw)
            except Exception:
                pass

        try:
            if asyncio.get_running_loop() is loop:
                _write()
            else:
                loop.call_soon_threadsafe(_write)
        except RuntimeError:
            try:
                loop.call_soon_threadsafe(_write)
            except Exception:
                pass

    async def _rpc(self, op: str, **fields) -> dict:
        if not await self.attach():
            return {"ok": False, "detail": "no broker"}
        writer = self._writer
        if writer is None:
            return {"ok": False, "detail": "no broker"}
        self._rpc_id += 1
        req_id = self._rpc_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut
        payload = {"op": op, "id": req_id, **fields}
        try:
            writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
            return await asyncio.wait_for(fut, BROKER_RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"ok": False, "detail": BROKER_TIMEOUT_DETAIL}
        except Exception as exc:
            self._pending.pop(req_id, None)
            return {"ok": False, "detail": str(exc) or "broker rpc failed"}


# ── the one host the daemon installs ─────────────────────────────────────────

_host: Optional[PtyHost] = None


def install(host: Optional[PtyHost]) -> None:
    """The daemon names the host every module-level question is asked of."""
    global _host
    _host = host


def current() -> Optional[PtyHost]:
    return _host


def owns(pid) -> Optional[str]:
    """`session_io`'s first question. None with no host installed."""
    host = _host
    if host is None:
        return None
    return host.owns(pid)


def send(handle: str, text: str, newline: bool = True) -> Optional[dict]:
    """Type into a Dark Army-owned terminal. The extension's `send_text` reply
    shape, so no caller changes: `{matched, sent, terminalName, matchedBy}`
    or None."""
    host = _host
    if host is None:
        return None
    term = host.get(handle)
    if term is None:
        return None
    if not host.write(handle, text + ("\r" if newline else "")):
        return None
    return {"matched": True, "sent": True, "terminalName": term.name,
            "matchedBy": "pty"}


async def close(handle: str) -> Optional[dict]:
    """Close a Dark Army-owned terminal. `close_terminal`'s reply shape."""
    host = _host
    if host is None:
        return None
    term = host.get(handle)
    if term is None:
        return None
    name = term.name
    if not await host.close(handle):
        return None
    return {"matched": True, "terminalName": name, "matchedBy": "pty"}


__all__ = ["PtyHost", "PtyTerminal", "install", "current", "owns", "send",
           "close", "RING_BYTES", "CLOSE_GRACE_SECONDS", "DEFAULT_COLS",
           "DEFAULT_ROWS", "TERM", "EXITED_RETENTION_SECONDS",
           "BROKER_LINE_LIMIT", "BROKER_RPC_TIMEOUT", "BROKER_TIMEOUT_DETAIL",
           "BROKER_LOST_DETAIL", "DATA_FRAME_BYTES", "HELLO_TERMINALS_HEADROOM",
           "HELLO_SCREEN_HEADROOM_BYTES", "RECONNECT_FIRST_SECONDS",
           "RECONNECT_MAX_SECONDS", "CONNECT_CONNECTED", "CONNECT_REFUSED",
           "CONNECT_UNREADABLE", "stray_broker_pids", "BROKER_MODULE",
           "MAX_STRAY_SWEEP"]
