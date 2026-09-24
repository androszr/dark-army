"""The kill switch: everything of Dark Army's that is running, stopped at once.

Quit is polite. It shuts the daemon down cleanly and deliberately *leaves*
the hosted terminals up, so a relaunch reconnects to them. This is the other
verb: one press and nothing of Dark Army's is left running on the machine.

**What it may kill is a proof, not a guess.** A process is a target only
because its own argv says it is running Dark Army's code:

* its executable lives inside the running `.app` bundle (the menu bar app
  itself, its bundled interpreter, the panel it launched);
* it runs one of Dark Army's own modules as two adjacent exact elements,
  ``-m dark_army_daemon.pty_broker`` and its siblings — or the same module
  under the package's name before the 22 Sep 2026 rename, which a broker the
  previous build started still runs;
* an argv element is exactly one of the helper scripts Dark Army installs
  into its state directory (the channel MCP server, the hook handler, the
  close-out shim);
* its executable is one the app can name outright — the panel binary it
  chose at startup, which in a checkout sits outside any bundle.

Every one of those is an exact element or an exact path, never a substring:
``grep``, an editor and this project's own tests all mention these names, and
a session whose *prompt* quotes ``~/.dark-army`` must not be signalled for
saying a word.

Then, and only then, the tree below each of those is swept as well. That is
what reaches the agents inside Dark Army's own hosted terminals: they are
started with ``start_new_session=True``, so killing the broker alone would
orphan them, and being the broker's child is the same kind of proof.

**What it never touches**: a `claude`, `codex` or `grok` session running in a
VS Code terminal. Dark Army asked the editor to open that tab; the process is
the editor's child and the person's work. It loses Dark Army and carries on.

The identification half is pure and table-testable — no psutil, no signals,
no clock. `snapshot()` and `terminate()` are the two impure functions, and
they are the only ones that touch the machine.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("dark-army.kill_switch")

#: Modules that are Dark Army's, run as ``-m <module>``. Matched as an exact
#: element or as an exact dotted prefix of one — `dark_army_daemon.x` is
#: ours, `dark_army_daemonish` is not.
OWN_PACKAGES = ("dark_army_daemon", "dark_army_menubar")

#: The scripts the app installs into its state directory. Matched as an exact
#: path (``<state dir>/<name>``), never by basename: a file of the same name
#: anywhere else on the machine is somebody else's.
STATE_HELPERS = (
    "dark-army-channel",
    "dark-army-notify",
    "dark-army-close-out",
)

#: The most processes one press will ever signal. A kill switch that fires a
#: thousand signals because a path rule went wrong is worse than one that
#: refuses and says so.
MAX_VICTIMS = 200

#: How long SIGTERM gets before SIGKILL.
GRACE_SECONDS = 1.5


@dataclass(frozen=True)
class Proc:
    """One process, as the identification rules see it."""

    pid: int
    ppid: int
    argv: tuple = ()
    #: Process start time, seconds since the epoch. Carried so the second
    #: signal can prove the pid is still the same process it was.
    started: float = 0.0


@dataclass
class Plan:
    """What one press would do. `victims` is deepest-first: a child is
    signalled before its parent, so nothing is respawned on the way down."""

    victims: list = field(default_factory=list)
    #: pid → why it is on the list, in plain words, for the log line.
    reasons: dict = field(default_factory=dict)
    #: True when the rules found more than `MAX_VICTIMS`. Nothing is
    #: signalled in that case: the list is the evidence something is wrong.
    overflowed: bool = False

    def __bool__(self) -> bool:
        return bool(self.victims)


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _inside(child: str, parent: str) -> bool:
    """Component-aware containment: `/a/proj` does not admit `/a/project2`.
    `workspace._contains`' rule, kept local so this module imports nothing
    from the daemon."""
    if not child or not parent:
        return False
    parent = parent.rstrip(os.sep)
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _module_run(argv: Sequence) -> str:
    """The module an argv runs as ``-m <module>``, when it is one of ours.

    Two adjacent exact elements, exactly as `ptyhost.stray_broker_pids` reads
    the same shape: a joined command line would match a `grep` for the name.
    """
    for i in range(len(argv) - 1):
        if argv[i] != "-m":
            continue
        module = argv[i + 1]
        for package in OWN_PACKAGES:
            if module == package or module.startswith(package + "."):
                return module
    return ""


def owns(argv: Sequence, *, bundle_root: str = "", state_dir: str = "",
         executables: Iterable = ()) -> str:
    """Why this argv is Dark Army's own code, or `""` when it is not.

    Pure. An empty `bundle_root` or `state_dir` turns that rule off rather
    than matching everything — a dev checkout has no bundle, and a caller
    that cannot name its state directory must not therefore kill the machine.
    """
    argv = [a for a in (argv or ()) if isinstance(a, str)]
    if not argv:
        return ""

    exe = _real(argv[0])
    if bundle_root and _inside(exe, _real(bundle_root)):
        return "app bundle"

    for named in executables or ():
        if isinstance(named, str) and named and exe == _real(named):
            return "panel"

    module = _module_run(argv)
    if module:
        return "module %s" % module

    if state_dir:
        wanted: dict = {}
        for name in STATE_HELPERS:
            # First name wins: an old name linked to a new file resolves to
            # the same path, and the reason should say the current name.
            wanted.setdefault(_real(os.path.join(state_dir, name)), name)
        for arg in argv:
            name = wanted.get(_real(arg))
            if name:
                return name

    return ""


def _children_of(procs: Sequence) -> dict:
    tree: dict = {}
    for proc in procs:
        tree.setdefault(proc.ppid, []).append(proc.pid)
    return tree


def plan(procs: Sequence, *, bundle_root: str = "", state_dir: str = "",
         executables: Iterable = (), own_pid: int = 0) -> Plan:
    """Everything one press would signal, deepest-first.

    `own_pid` — the pressing process — is left off the list entirely: it ends
    itself, last, after the signals have gone out. Its descendants stay on.
    """
    by_pid = {}
    for proc in procs or ():
        if not isinstance(proc.pid, int) or isinstance(proc.pid, bool):
            continue
        if proc.pid <= 1:      # never init, never a nonsense pid
            continue
        by_pid[proc.pid] = proc

    result = Plan()
    roots: list = []
    for pid, proc in by_pid.items():
        reason = owns(proc.argv, bundle_root=bundle_root, state_dir=state_dir,
                      executables=executables)
        if reason:
            roots.append(pid)
            result.reasons[pid] = reason

    children = _children_of(by_pid.values())
    frontier = list(roots)
    seen = set(roots)
    while frontier:
        if len(seen) > MAX_VICTIMS:
            result.overflowed = True
            result.victims = []
            return result
        pid = frontier.pop(0)
        for child in children.get(pid, ()):
            if child in seen or child <= 1:
                continue
            seen.add(child)
            result.reasons.setdefault(child, "child of %d" % pid)
            frontier.append(child)

    # How far below the topmost target each pid sits, so the sort can put a
    # child ahead of its parent. Computed from the ppid chain rather than on
    # the way down, because a target may itself be another target's child
    # (the broker under the app) and would otherwise stay at depth 0.
    depth: dict = {}

    def _depth(pid: int, guard: set) -> int:
        if pid in depth:
            return depth[pid]
        if pid in guard:          # a ppid cycle the kernel should not give us
            return 0
        proc = by_pid.get(pid)
        parent = proc.ppid if proc is not None else 0
        if parent not in seen:
            depth[pid] = 0
            return 0
        guard.add(pid)
        depth[pid] = _depth(parent, guard) + 1
        return depth[pid]

    for pid in seen:
        _depth(pid, set())

    order = sorted(seen, key=lambda pid: (-depth.get(pid, 0), pid))
    result.victims = [by_pid[pid] for pid in order
                      if pid in by_pid and pid != own_pid]
    result.reasons.pop(own_pid, None)
    return result


# --- The two impure halves ------------------------------------------------


def snapshot() -> list:
    """Every process this user owns, as `Proc`s. Whole-machine walk; call it
    off the main thread. An unreadable field is a skip, never a guess."""
    import psutil                        # local: heavy, and only needed here

    me = os.getuid()
    out: list = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "uids",
                                     "create_time"]):
        try:
            info = proc.info
            uids = info.get("uids")
            # An unreadable uid is not ownership: over-excluding is the safe
            # direction for something that ends in a signal.
            if getattr(uids, "real", None) != me:
                continue
            pid = info.get("pid")
            ppid = info.get("ppid")
            if not isinstance(pid, int) or not isinstance(ppid, int):
                continue
            out.append(Proc(pid=pid, ppid=ppid,
                            argv=tuple(info.get("cmdline") or ()),
                            started=float(info.get("create_time") or 0.0)))
        except psutil.Error:
            continue
    return out


def _same_process(proc: Proc) -> bool:
    """Is this pid still the process the plan named? Start time is the
    identity, the notify script's rule: between SIGTERM and SIGKILL the
    machine may have recycled the number."""
    if not proc.started:
        return True
    try:
        import psutil

        return abs(psutil.Process(proc.pid).create_time() - proc.started) < 0.5
    except Exception:
        return False


def terminate(victims: Sequence, *,
              grace: float = GRACE_SECONDS,
              kill: Callable = os.kill,
              sleep: Callable = time.sleep,
              still_alive: Callable = _same_process) -> list:
    """SIGTERM every victim, wait `grace`, then SIGKILL whichever is both
    still there and still the same process. One pid at a time; never a
    process group, whose membership this module has not proved."""
    sent: list = []
    for proc in victims:
        try:
            kill(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.info("kill switch: %d not signalled: %s", proc.pid, exc)
            continue
        sent.append(proc)
    if not sent:
        return []
    sleep(grace)
    for proc in sent:
        if not still_alive(proc):
            continue
        try:
            kill(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return [proc.pid for proc in sent]


__all__ = ["Proc", "Plan", "OWN_PACKAGES", "STATE_HELPERS", "MAX_VICTIMS",
           "GRACE_SECONDS", "owns", "plan", "snapshot", "terminate"]
