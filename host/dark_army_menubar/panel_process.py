"""Lifecycle for the native combo panel (the Swift `BobPanel` executable).

The panel is a separate process, held open and driven over its stdin, rather
than launched per click. Spawning it each time would cost a process start, a
Swift runtime init and a fresh SSE connection before anything appeared — the
same reason the simulator was kept alive hidden instead of respawned.

Only this module knows where the status item ended up, which is why the anchor
point is *sent* rather than discovered: the item moves whenever the strip's width
changes, and the panel has no way to ask.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("dark-army")

EXECUTABLE_NAME = "BobPanel"


def find_executable() -> Optional[str]:
    """The panel binary: bundled first, then a dev build in the checkout.

    Release before debug — a checkout that has built both should run the one that
    is not carrying a debugger's overhead.

    **Never gate the bundled path on `sys.frozen`.** Only the bundle's
    `__boot__.py` sets it, and `_on_restart` relaunches the app as
    `sys.executable -m dark_army_menubar` — the bundled interpreter, booted
    without that script. So the flag is set on the first launch and absent on
    every launch after a Restart, and this returned None from inside a bundle
    that had the binary sitting in Contents/Resources. The symptom was "The panel
    is not built" on an app that had just built it. `dev_build.is_frozen()`
    carries the same warning; this module learned it the same way.

    So the bundle layout is simply *tried*: if a sibling Resources directory has
    the binary, that is the answer regardless of how the interpreter was started.
    """
    resources = Path(sys.executable).resolve().parent.parent / "Resources"
    # The nested .app first. The panel needs a bundle *directory* around it or
    # LaunchServices gives the running process no identifier, and every other
    # app on the machine — a dictation tool asking which app is in front, above
    # all — sees a nameless process where the window is. The bare executable
    # beside it is the older layout, kept so a menu bar from before the change
    # still finds something to run.
    # The Dock labels a directly launched binary by its file name. The
    # installed copy is named Dark Army so the tile says that; BobPanel is
    # the Swift product and the older bundle layout.
    for bundled in (
        resources / "Dark Army.app" / "Contents" / "MacOS" / "Dark Army",
        resources / f"{EXECUTABLE_NAME}.app" / "Contents" / "MacOS" / "Dark Army",
        resources / f"{EXECUTABLE_NAME}.app" / "Contents" / "MacOS" / EXECUTABLE_NAME,
        resources / EXECUTABLE_NAME,
    ):
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)

    here = Path(__file__).resolve()
    for parent in here.parents:
        for build in ("release", "debug"):
            candidate = parent / "panel" / ".build" / build / EXECUTABLE_NAME
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        if (parent / ".git").exists():
            break
    return shutil.which(EXECUTABLE_NAME)


class PanelProcess:
    """Starts the panel on first use and keeps it running."""

    #: How many lines may wait for the panel before the oldest is shed. Context
    #: pushes are idempotent snapshots — the newest one wins — so a bounded
    #: buffer that drops from the front loses nothing a healthy panel would
    #: miss, and a *hung* panel costs us at most this many queued lines rather
    #: than the AppKit thread.
    QUEUE_MAX = 64

    def __init__(self, on_action: Optional[Callable[[str, object], None]] = None) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._path = find_executable()
        # Rebuild, restart and quit act on the *app*, not on the daemon, so the
        # panel cannot do them itself — it asks. Commands go in on stdin and
        # requests come back on stdout, one JSON object per line.
        self._on_action = on_action
        self._reader: Optional[threading.Thread] = None
        # `_proc` is swapped from the AppKit thread (`_send` → `_spawn`), from
        # a worker thread (`_restart_now` → `quit`) and from the sender thread
        # (a broken pipe). Every swap happens under this lock; the blocking
        # write itself never does.
        self._lock = threading.RLock()
        # The outbox the sender thread drains. A deque under a condition
        # rather than a `queue.Queue`, because the overflow policy needs to
        # *scan*: the oldest non-quit line is the one shed, never a quit.
        self._outbox: deque = deque()
        self._outbox_cond = threading.Condition()
        self._sender: Optional[threading.Thread] = None
        self._sender_stop = threading.Event()

    @property
    def executable_path(self) -> Optional[str]:
        """The executable selected for this instance, before or after a spawn.

        Status reads share this saved choice; only a new instance resolves it
        again. This names the file on disk, not bytes loaded by a live process.
        """
        return self._path

    @property
    def available(self) -> bool:
        return self._path is not None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def alive(self) -> bool:
        """Whether a panel process is running right now. Public so a periodic
        context push can skip a dead panel rather than respawning it — `_send`
        spawns on a dead panel, which turns a 30s timer into a crash loop."""
        return self._alive()

    def _spawn(self) -> bool:
        with self._lock:
            if not self._path:
                return False
            try:
                self._proc = subprocess.Popen(
                    [self._path, "--hidden"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    # Not DEVNULL: a panel that dies on launch is otherwise
                    # indistinguishable from one that is working, and a silent
                    # exit is one of the two ways the two processes' idea of
                    # "visible" can drift apart.
                    stderr=subprocess.PIPE,
                    start_new_session=False,  # dies with us; see main.swift on EOF
                )
            except OSError as exc:
                logger.warning("Could not start the panel: %s", exc)
                self._proc = None
                return False
            logger.info("Panel started (pid %s)", self._proc.pid)
            self._reader = threading.Thread(target=self._read_events, args=(self._proc,),
                                            name="panel-events", daemon=True)
            self._reader.start()
            threading.Thread(target=self._read_errors, args=(self._proc,),
                             name="panel-stderr", daemon=True).start()
            self._start_sender()
            return True

    def _start_sender(self) -> None:
        """A fresh outbox and sender thread for the process in `_proc`.

        The previous sender (if any) is told to stop and its outbox abandoned:
        lines queued for a dead panel must never be replayed into a new one,
        where a stale `show`'s anchor or a `quit` would be actively wrong.
        Called under `_lock` (or before any concurrency exists, in tests).
        """
        self._sender_stop.set()
        with self._outbox_cond:
            self._outbox_cond.notify_all()
        self._sender_stop = threading.Event()
        self._outbox = deque()
        self._sender = threading.Thread(
            target=self._drain_outbox,
            args=(self._proc, self._outbox, self._outbox_cond, self._sender_stop),
            name="panel-sender", daemon=True)
        self._sender.start()

    def _drain_outbox(self, proc: subprocess.Popen, outbox: deque,
                      cond: threading.Condition, stop: threading.Event) -> None:
        """The one thread that writes the panel's stdin.

        The pipe is blocking and the panel's main thread is the reader; a
        panel wedged on a sheet stops draining and the 64 KB pipe fills. That
        stall lands *here*, on a daemon thread, instead of freezing the AppKit
        thread mid-timer, which is the whole point of the queue.
        """
        stdin = proc.stdin
        if stdin is None:
            return
        while True:
            with cond:
                while not outbox and not stop.is_set():
                    cond.wait()
                if stop.is_set():
                    return
                line, _is_quit = outbox.popleft()
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError):
                # It died (or was rebuilt) underneath us. Mark it dead and
                # stand down: the next `_send` spawns a fresh panel — the
                # caller side owns respawning, never this thread.
                self._mark_dead(proc, stop)
                return

    def _mark_dead(self, proc: subprocess.Popen, stop: threading.Event) -> None:
        stop.set()
        with self._lock:
            if self._proc is proc:
                logger.info("Panel pipe broke; it will be restarted on next use")
                self._proc = None

    def _enqueue(self, payload: dict) -> bool:
        """Queue one line for the sender. Never blocks.

        On overflow the *oldest non-quit* line is shed — a context push is a
        snapshot and the newest wins — and a `quit` is never dropped: it is
        the one line whose loss would strand a process.
        """
        line = (json.dumps(payload) + "\n").encode()
        is_quit = payload.get("action") == "quit"
        with self._outbox_cond:
            outbox = self._outbox
            if len(outbox) >= self.QUEUE_MAX:
                for i in range(len(outbox)):
                    if not outbox[i][1]:
                        del outbox[i]
                        break
                else:
                    # Nothing but quits queued (pathological). A quit still
                    # goes on; anything else is the drop.
                    if not is_quit:
                        return True
            outbox.append((line, is_quit))
            self._outbox_cond.notify()
        return True

    @staticmethod
    def _read_errors(proc: subprocess.Popen) -> None:
        """Anything the panel writes to stderr goes into the app log.

        Its own flight recorder (`Trace.swift`) writes there too, marked, and is
        filed as information rather than as a warning: those lines are the
        expected traffic of a healthy panel, and a log where every routine line
        says WARNING is a log nobody reads the warnings in. Everything else on
        stderr is still a warning — a Swift runtime complaint has not changed
        its meaning.
        """
        stream = proc.stderr
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if line.startswith("trace "):
                logger.info("panel: %s", line[len("trace "):])
            else:
                logger.warning("panel: %s", line)

    def _read_events(self, proc: subprocess.Popen) -> None:
        """Drain the panel's stdout. One JSON object per line; anything else is
        ignored, because a Swift runtime warning on stdout must not be able to
        trigger an app action."""
        stream = proc.stdout
        if stream is None:
            return
        for raw in stream:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, AttributeError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("event") == "action" and self._on_action:
                name = str(message.get("name") or "")
                if name:
                    try:
                        # `value` is optional and only the settings verbs carry
                        # one — a toggle sends the state it wants rather than
                        # asking the app to flip whatever it currently holds,
                        # which is the difference between a switch that is right
                        # and one that is right most of the time.
                        self._on_action(name, message.get("value"))
                    except Exception:
                        logger.exception("Panel action %r failed", name)
        # EOF: the panel quit or died. A stale "visible" reading must never
        # outlive the process that reported it — this is the fail-open half of
        # the docked sidebar's alert-suppression contract (the daemon's
        # PANEL_VISIBLE_TRUST_SECONDS TTL is the other), so banners come back
        # the moment the pipe closes instead of 15 seconds later. `quit()`
        # needs nothing extra: killing the process closes the pipe and lands
        # here.
        if self._on_action:
            try:
                self._on_action("panel_visibility", False)
            except Exception:
                logger.exception("Could not clear panel visibility on EOF")
            try:
                self._on_action("dictate_up", None)
            except Exception:
                logger.exception("Could not release dictation on EOF")

    def _send(self, payload: dict) -> bool:
        """Enqueue one line for the panel, spawning it first if it is dead.

        This runs on the AppKit thread and must never block: the write and
        flush live on the sender thread (`_drain_outbox`), which is the only
        thing a panel that has stopped draining its stdin can wedge. True
        means queued, not delivered — delivery was always best-effort here
        (the old direct write could break mid-pipe too).
        """
        with self._lock:
            if not self._alive() and not self._spawn():
                return False
        return self._enqueue(payload)

    def set_context(self, **fields) -> bool:
        """Tell the panel the things only the menu bar knows — build staleness,
        the Grok window (a different account than the one `/api/usage` reports
        on), and every preference, since the panel owns the settings UI.

        It also carries ``desk_token``, the loopback door's desk token
        (`docs/transport-contract.md`, *The loopback door has two tokens*):
        this pipe is the only road it takes to the panel, because a third
        process can read neither side of it — unlike a file, the environment
        or an argv. So **no payload is ever logged** on this path: `_send`,
        `_enqueue` and `_drain_outbox` log a broken pipe and nothing else, and
        `test_menubar.py` pins that the value reaches no log line."""
        return self._send({"action": "context", **fields})

    @staticmethod
    def _with_anchor(payload: dict, anchor: Optional[tuple]) -> dict:
        """Add the status item's frame `(x, y, w, h)` to a command.

        The whole rect, not just the corner the panel hangs from: the panel needs
        it to tell a dismissing click *on the status item* from a click anywhere
        else. Without that it hides on the click and the menu bar's own toggle,
        arriving a millisecond later, re-opens it — which is exactly what "the
        second click doesn't hide it" was."""
        if anchor:
            x, y = anchor[0], anchor[1]
            w = anchor[2] if len(anchor) > 2 else 0.0
            h = anchor[3] if len(anchor) > 3 else 0.0
            payload.update({"x": x + w, "y": y,
                            "ax": x, "ay": y, "aw": w, "ah": h})
        return payload

    def toggle(self, anchor: Optional[tuple] = None) -> bool:
        """Show or hide, from a click on the status item."""
        return self._send(self._with_anchor({"action": "toggle"}, anchor))

    def show(self, anchor: Optional[tuple] = None,
             focus: Optional[str] = None, card: bool = False) -> bool:
        """Open the panel — never toggle it — optionally on one session.

        `focus` is a session id, and it is what a tap on a notification banner
        sends: the panel selects that row, drills to wherever it lives and
        unfolds what the agent said. Not `toggle`, because the answer to "take
        me to this agent" is never to close the panel.

        `card` additionally asks the board to scroll that session's card into
        view and glow it — the reverse jump from VS Code, which is aimed at the
        work rather than at an interruption. Additive and written only when
        set, so the banner tap's payload stays byte for byte what it was and an
        older panel binary simply ignores the key."""
        payload: dict = {"action": "show"}
        if focus:
            payload["focus"] = focus
        if card:
            payload["card"] = True
        return self._send(self._with_anchor(payload, anchor))

    def hide(self) -> bool:
        return self._send({"action": "hide"}) if self._alive() else True

    def quit(self) -> None:
        with self._lock:
            proc = self._proc
            stop = self._sender_stop
        if proc is None or proc.poll() is not None:
            return
        try:
            # The quit line rides the same queue — `_enqueue` guarantees it a
            # place even when the queue is full — and the bounded `wait` below
            # is the deadline for the sender to deliver it and the panel to
            # act. A panel too wedged to drain its pipe misses the deadline
            # and is killed, exactly as before.
            self._send({"action": "quit"})
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        stop.set()
        with self._outbox_cond:
            self._outbox_cond.notify_all()
        with self._lock:
            if self._proc is proc:
                self._proc = None


__all__ = ["PanelProcess", "find_executable", "EXECUTABLE_NAME"]
