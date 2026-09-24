"""Launch at login: Dark Army's per-user LaunchAgent.

The login item is one plist under `~/Library/LaunchAgents` naming this
interpreter and the menu-bar module. Every `launchctl` call goes through the
module's own `subprocess`, so a test stands in for all of them at once.
"""

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("dark-army.launchd")

PLIST_LABEL = "com.dark-army.menubar"
PLIST_PATH = Path.home().joinpath("Library", "LaunchAgents", PLIST_LABEL + ".plist")


def _launchctl(verb: str, plist: Path):
    """`launchctl <verb> gui/<uid> <plist>`, output captured, never raised on."""
    return subprocess.run(["launchctl", verb, f"gui/{os.getuid()}", str(plist)],
                          capture_output=True)


def _job_pid(label: str):
    """The pid launchd says is running `label`, or None when it says none or
    cannot be read. `launchctl print` is the one place that names it."""
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    out = proc.stdout or b""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    for line in out.splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "pid":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def is_enabled() -> bool:
    """True while our login item's plist is on disk (loaded or not)."""
    return PLIST_PATH.exists()


class EnableResult(NamedTuple):
    """What `enable()` actually did, so a caller can tell a plist on disk
    from an agent launchd agreed to run.

    `written` is the plist; `bootstrapped` is `launchctl bootstrap` exiting
    0. The two used to be indistinguishable — `is_enabled()` proves only that
    the file exists — which is how a launch agent that never loaded looked
    exactly like one that did. `detail` is one safe sentence, never stderr
    verbatim (that goes to the log).
    """

    written: bool
    bootstrapped: bool
    detail: str


#: What the login item runs after the interpreter. `is_stale` compares the
#: plist's whole tail against it, not only the interpreter: the interpreter of
#: an installed app did not change when the package was renamed on
#: 22 Sep 2026, and a plist still naming the old module would start nothing
#: at the next login.
PROGRAM_ARGUMENTS_TAIL = ("-m", "dark_army_menubar.app")


def _write_plist() -> None:
    """The login item's plist for this executable, loaded by nobody."""
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [sys.executable, *PROGRAM_ARGUMENTS_TAIL],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    PLIST_PATH.write_bytes(plistlib.dumps(plist))


def enable() -> EnableResult:
    """Write the launchd plist and load the agent.

    Returns an `EnableResult`. Every existing caller ignores the return value
    and keeps its behavior; the first-run report reads it.
    """
    _write_plist()

    # Unload before loading: a label launchd already holds (a refresh of a
    # stale plist) makes `bootstrap` refuse, and the old program path would
    # keep running until the next login. Unloading a label that is not loaded
    # fails harmlessly; only the bootstrap's answer is read.
    _launchctl("bootout", PLIST_PATH)
    proc = _launchctl("bootstrap", PLIST_PATH)
    if proc.returncode != 0:
        logger.warning(
            "launchctl bootstrap of %s exited %d: %s",
            PLIST_PATH, proc.returncode,
            (proc.stderr or b"").decode("utf-8", "replace").strip(),
        )
        return EnableResult(
            True, False,
            f"login item written but launchd did not register it "
            f"(bootstrap exited {proc.returncode})")
    return EnableResult(True, True, "login item enabled")


#: What the stale-plist repair reports: written, not loaded, and when it bites.
#: The panel's launch line draws a `changed` detail naming the next login
#: verbatim (`FirstRunChecklist.launchLine`), so it never reads "enabled".
REPAIRED_DETAIL = "login item updated, effective at the next login"


def repair_stale_login_item() -> EnableResult:
    """Rewrite a login item that names another interpreter or module.

    Called from the first launch after an app move or a package rename.
    **Never `launchctl bootstrap` here**: this runs inside the launch, and
    bootstrapping a `RunAtLoad` job would start a second copy of the app.
    The old job is booted out — unless its running pid is this process,
    which a bootout would end — and the new plist is written; it takes
    effect at the next login."""
    if _job_pid(PLIST_LABEL) != os.getpid():
        _launchctl("bootout", PLIST_PATH)
    else:
        logger.info("Not booting out %s: it is this process", PLIST_LABEL)
    _write_plist()
    return EnableResult(True, False, REPAIRED_DETAIL)


def _recorded_arguments():
    """Every program argument our plist names, or None when there is no
    plist, it cannot be read, or it names nothing."""
    try:
        recorded = plistlib.loads(PLIST_PATH.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    argv = recorded.get("ProgramArguments") or []
    return list(argv) if argv else None


def _recorded_interpreter():
    """The first program argument our plist names, or None when there is no
    plist, it cannot be read, or it names nothing."""
    argv = _recorded_arguments()
    return argv[0] if argv else None


def is_stale() -> bool:
    """True when the login item would start something other than this app:
    another interpreter (an app moved or rebuilt since the plist was written)
    or another module (a plist written before the 22 Sep 2026 package rename
    still names the old package's module). No plist is never stale."""
    argv = _recorded_arguments()
    if argv is None:
        return False
    return argv[0] != sys.executable or argv[1:] != list(PROGRAM_ARGUMENTS_TAIL)


def disable() -> None:
    """Unload the login item and delete its plist; nothing to do without one."""
    if PLIST_PATH.exists():
        _launchctl("bootout", PLIST_PATH)
        PLIST_PATH.unlink(missing_ok=True)
