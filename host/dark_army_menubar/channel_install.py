"""Putting Dark Army's channel where a session can start it.

The channel itself is `dark_army_daemon/channel_server.py`. This installs a
copy of it beside the notify script and registers that copy with Claude Code, so
a session can be started with:

    claude --dangerously-load-development-channels server:dark-army

**Registered twice, for the dual-name window.** The one copy is added under
`dark-army` (`--name=dark-army`) and under the legacy `bob` (`--name=bob`), so
a session born before the rename keeps its `bob_*` tools and a launch line
naming `server:bob` still resolves. Only one of the two processes a session
spawns is active; the other answers with no tools (`channel_server.is_active`).
The follow-up *End the channel dual-name window* drops the `bob` registration.

The same copy is also registered with Codex under Dark Army's dedicated
`dark-army-board` name, with `--host=codex` selecting the restricted
board-only surface. Codex's CLI owns that registration; Dark Army never edits its
configuration file and never replaces an entry it cannot prove it owns — which
is also the rule for the old `bob-companion-board` entry, removed only when it
is exactly the one the previous build wrote.

**`--mcp-config` does not work here, and finding that out cost a probe.** The
obvious install — write a one-server config of our own and name it on the
command line — leaves the flag unable to resolve the name: a clean run in a
directory with no `.mcp.json` answered `server:bob · no MCP server configured
with that name` and never spawned the server at all, while the same file placed
as a project `.mcp.json` worked. The development-channels flag resolves against
the *configured* servers, not against a config passed beside it.

**So it is registered at user scope, through `claude mcp add`.** Not by editing
`~/.claude.json` ourselves: that file is Claude Code's own hot state — every
session writes to it — and a read-modify-write from here would eventually land
on top of somebody's, losing whatever they had just recorded. The CLI owns that
file and serialises its own writes; the cost is a subprocess, which this pays
once per toggle.

The consequence to know about: at user scope the server is spawned by *every*
session, and only the ones started with the flag register it as a channel. That
is a few hundred KB of idle Python per session, and it means a session can
announce a channel Dark Army cannot actually push through — an event sent to it is
dropped by the harness, silently and by design. Dark Army therefore treats channels as
best-effort delivery and never as a promise, which is what `push_channel_event`
returning False already says.

**Why a copy of the script rather than the file in the app bundle.** The same
reason `dark-army-notify` is written out: the path inside a `.app` moves
with every rebuild and vanishes when the app is replaced, and the registration
holds an absolute command. Copied rather than embedded as a string, though — the
notify script is embedded and grew a second divergent copy on disk, which is the
mistake this avoids by having exactly one source file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

from dark_army_daemon import channel_server
from dark_army_daemon.agents_poll import find_claude_binary
from dark_army_daemon.dispatch import NOT_INSTALLED_REFUSAL, resolve_executable
from dark_army_daemon.paths import CHANNEL_SCRIPT_NAME, STATE_DIR

logger = logging.getLogger("dark-army.channel")

CHANNEL_SCRIPT = STATE_DIR / CHANNEL_SCRIPT_NAME

#: The name `--dangerously-load-development-channels server:<name>` has to
#: match, and the `source=` every event carries into the session.
SERVER_NAME = channel_server.CURRENT_NAME
#: The name every machine registered before the rename, kept for the
#: dual-name window beside `SERVER_NAME`.
LEGACY_SERVER_NAME = channel_server.LEGACY_NAME
#: Both registrations, current first. `install` removes every one of them
#: before adding any, and checks each remove (see `install`), so no moment
#: holds the new flagged `dark-army` beside the previous build's flagless
#: `bob` (both would be active); the cost is a brief window, between the
#: removes and the first add, with no board tools.
SERVER_NAMES = (SERVER_NAME, LEGACY_SERVER_NAME)

#: Dark Army owns exactly this Codex MCP name. It is deliberately different from the
#: Claude development-channel name: Codex gets a board-only MCP surface, never
#: something that could be mistaken for a bidirectional session channel.
CODEX_SERVER_NAME = "dark-army-board"
#: The Codex name before the rename. Removed where the previous build's exact
#: entry is found, left (with a warning) where it is somebody else's.
LEGACY_CODEX_SERVER_NAME = "bob-companion-board"

CLI_TIMEOUT_SECONDS = 20.0


def _interpreter() -> str:
    """A python that will still exist tomorrow.

    Not `sys.executable`: inside the frozen app that is the bundle's own binary,
    which is not a general interpreter and is replaced on the next build. The
    channel is stdlib-only precisely so the system python can run it.
    """
    for candidate in (shutil.which("python3"), "/usr/bin/python3"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/usr/bin/python3"


def source_path() -> Path:
    return Path(channel_server.__file__)


def launch_command() -> str:
    """What a session has to be started with. One line, meant to be pasted."""
    return f"claude --dangerously-load-development-channels server:{SERVER_NAME}"


def _claude(*args: str) -> tuple[bool, str]:
    binary = find_claude_binary()
    if not binary:
        return False, NOT_INSTALLED_REFUSAL.format(tool="claude")
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, (proc.stdout or "").strip()


def _remove_claude(name: str) -> bool:
    """Remove one Claude Code registration; True when the name is now absent.

    Three outcomes. The CLI succeeded: True. The CLI said the name was never
    registered (exit 1, stderr exactly `No MCP server named "<name>" in user
    scope`): also True, there is nothing to remove, as on a fresh machine.
    Anything else, a timeout, a missing binary or any other refusal: False.
    A timeout is not "probably removed": the CLI may have hung before it
    removed anything, and an unverified removal followed by an add is exactly
    the two-tool-lists shape the remove-first order exists to prevent.
    """
    ok, detail = _claude("mcp", "remove", "-s", "user", name)
    if ok:
        return True
    if "no mcp server named" in detail.lower():
        logger.debug("%s was not registered with Claude Code; nothing to "
                     "remove", name)
        return True
    logger.error("Could not remove the %s channel registration from Claude "
                 "Code; leaving the registrations as they are: %s", name, detail)
    return False


def _codex(*args: str) -> tuple[bool, str]:
    """Run the optional Codex CLI, bounded like the Claude registration.

    Codex owns its config file, so this wrapper is the only route to its MCP
    registry. Absence is a normal outcome: Claude session linking must continue
    to work on machines that have never installed Codex.
    """
    binary = resolve_executable("codex")
    if not binary:
        return False, NOT_INSTALLED_REFUSAL.format(tool="codex")
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, (proc.stdout or "").strip()


def _claude_command(name: str) -> list[str]:
    """The command one Claude registration holds: the copy, and its name."""
    return [_interpreter(), str(CHANNEL_SCRIPT), f"--name={name}"]


def _codex_command() -> tuple[str, list[str]]:
    return _interpreter(), [str(CHANNEL_SCRIPT), "--host=codex",
                            f"--name={channel_server.CURRENT_NAME}"]


def _legacy_codex_args() -> list[str]:
    """The arguments the build before the rename registered under
    `bob-companion-board`: the same copy, no name."""
    return [str(CHANNEL_SCRIPT), "--host=codex"]


def _codex_registration(name: str = CODEX_SERVER_NAME,
                        owned_args=None) -> tuple[str, str]:
    """Return ``owned``, ``absent``, ``foreign`` or ``error`` plus detail.

    A shared name is not ownership. Dark Army may replace/remove the entry only when
    both the command and every argument are exactly the values it would write
    under `name` — `owned_args`, a list of the argument lists that count,
    defaulting to what `_codex_command` writes today.
    Malformed output fails closed rather than turning a parser error into a
    destructive ``mcp remove``.
    """
    command, args = _codex_command()
    owned = [args] if owned_args is None else list(owned_args)
    ok, detail = _codex("mcp", "get", name, "--json")
    if not ok:
        lower = detail.lower()
        if "no mcp server named" in lower or "not found" in lower:
            return "absent", detail
        return "error", detail
    try:
        entry = json.loads(detail)
        transport = entry.get("transport") if isinstance(entry, dict) else None
        if (isinstance(transport, dict)
                and transport.get("type") == "stdio"
                and transport.get("command") == command
                and transport.get("args") in owned):
            return "owned", detail
    except (TypeError, ValueError):
        return "error", "Codex returned malformed JSON for the MCP entry."
    return "foreign", detail


def _retire_legacy_codex() -> None:
    """Remove the pre-rename `bob-companion-board` entry, only when owned.

    Codex spawns every registered server, so an entry left beside
    `dark-army-board` gives a Codex session both tool lists. The ownership
    rule still wins: a foreign entry is logged and left, and an unreadable one
    is skipped.
    """
    state, detail = _codex_registration(LEGACY_CODEX_SERVER_NAME,
                                        [_legacy_codex_args()])
    if state == "owned":
        ok, remove_detail = _codex("mcp", "remove", LEGACY_CODEX_SERVER_NAME)
        if not ok:
            logger.error("Could not remove the old Codex board helper: %s",
                         remove_detail)
    elif state == "foreign":
        logger.warning("Leaving foreign Codex MCP entry %s untouched",
                       LEGACY_CODEX_SERVER_NAME)
    elif state == "error" and "not installed on this mac" not in detail.lower():
        logger.info("Could not inspect the old Codex board helper: %s", detail)


def _install_codex() -> bool:
    _retire_legacy_codex()
    state, detail = _codex_registration()
    if state == "foreign":
        logger.error("Refusing to replace foreign Codex MCP entry %s",
                     CODEX_SERVER_NAME)
        return False
    if state == "error":
        logger.info("Codex board helper not installed: %s", detail)
        return False
    if state == "owned":
        ok, remove_detail = _codex("mcp", "remove", CODEX_SERVER_NAME)
        if not ok:
            logger.error("Could not refresh Codex board helper: %s", remove_detail)
            return False
    command, args = _codex_command()
    ok, add_detail = _codex("mcp", "add", CODEX_SERVER_NAME, "--",
                            command, *args)
    if not ok:
        logger.info("Codex board helper not installed: %s", add_detail)
        return False
    logger.info("Codex board helper installed as %s", CODEX_SERVER_NAME)
    return True


def _uninstall_codex() -> None:
    _retire_legacy_codex()
    state, detail = _codex_registration()
    if state == "owned":
        ok, remove_detail = _codex("mcp", "remove", CODEX_SERVER_NAME)
        if not ok:
            logger.error("Could not remove Codex board helper: %s", remove_detail)
    elif state == "foreign":
        logger.warning("Leaving foreign Codex MCP entry %s untouched",
                       CODEX_SERVER_NAME)
    elif state == "error" and "not installed on this mac" not in detail.lower():
        logger.info("Could not inspect Codex board helper during uninstall: %s",
                    detail)


def _write_script(text: str) -> bool:
    """Copy the server out of the package. False if it could not be written.

    Reported rather than assumed: py2app copies `packages` as a real directory
    tree, so the `.py` is there — but a packaging change that left only the
    `.pyc` would otherwise register a command pointing at a file that does not
    exist, and that surfaces as a session with no channel and no reason given.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CHANNEL_SCRIPT.write_text(text, encoding="utf-8")
        CHANNEL_SCRIPT.chmod(CHANNEL_SCRIPT.stat().st_mode | stat.S_IXUSR)
    except OSError:
        logger.exception("Could not write the channel script")
        return False
    return True


def _marker_path() -> Path:
    """Where the last successful install is recorded. A function rather than a
    module constant so it follows STATE_DIR wherever a test points it."""
    return STATE_DIR / "channel-install.json"


def _install_signature(text: str) -> dict:
    """What an install of `text` would amount to: the script's content digest
    plus the exact command each registration would hold. A marker of the
    previous shape (one `claude_command`) never matches, so the first launch
    of this build re-registers under both names — intended."""
    return {
        "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "claude_commands": {name: _claude_command(name) for name in SERVER_NAMES},
    }


def _already_installed(signature: dict) -> bool:
    """True when the last recorded install matches `signature` *and* the copy
    on disk still carries that content. The marker alone is not trusted: a
    hand-deleted or hand-edited script must fall through to a real install."""
    try:
        recorded = json.loads(_marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if recorded != signature:
        return False
    try:
        on_disk = hashlib.sha256(CHANNEL_SCRIPT.read_bytes()).hexdigest()
    except OSError:
        return False
    return on_disk == signature["digest"]


def _record_install(signature: dict) -> None:
    try:
        _marker_path().write_text(json.dumps(signature, indent=2) + "\n",
                                  encoding="utf-8")
    except OSError:
        # Best-effort: a missing marker only costs a re-install next launch.
        logger.debug("Could not record the channel install", exc_info=True)


def install(force: bool = False) -> bool:
    """Install the copy and register it — skipped entirely when the last
    recorded install is still current.

    The skip matters because this runs at every launch: both halves shell out
    (`claude mcp` four times — a remove and an add per name — and up to five
    `codex mcp`, each with a 20s timeout), and
    an unchanged script does not need any of it. `force=True` is the deliberate
    toggle's route and re-runs the CLI unconditionally — it is also the repair
    for a registration removed behind the marker's back.

    A failed remove is a failed install: nothing is added and no marker is
    recorded, so the next launch retries. Removes run legacy first, so a
    failed one never leaves only the passive `bob` registration behind.
    """
    try:
        text = source_path().read_text(encoding="utf-8")
    except OSError:
        logger.error("Channel source unreadable at %s — not installing",
                     source_path())
        return False
    signature = _install_signature(text)
    if not force and _already_installed(signature):
        return True
    if not _write_script(text):
        return False
    # Idempotent by removal first: `claude mcp add` refuses a name that is
    # already registered, and re-registering is the ordinary case — the script's
    # interpreter path can change under an upgrade. **Every** name is removed
    # before any is added: the previous build's flagless `bob` is active in a
    # plain session (`channel_server.is_active`), so `dark-army` must never be
    # added beside it (two tool lists and a displaced attach). The order alone
    # does not guarantee that, so a remove that fails for any reason other than
    # the name not being registered stops the install here, before any add; the
    # next launch (or the toggle) retries. The removes run legacy first: a
    # failed `bob` remove then touches nothing, and a failed `dark-army` remove
    # leaves only the `dark-army` copy, which is active in a plain session. (The
    # other way round, a failed `bob` remove would leave only the passive
    # `--name=bob` copy and those sessions would have no board tools.) What
    # remains is a brief window, between the removes and the first add, in
    # which a session starting has no board tools; current name added first
    # keeps it to one CLI call.
    for name in reversed(SERVER_NAMES):
        if not _remove_claude(name):
            return False
    for name in SERVER_NAMES:
        ok, detail = _claude("mcp", "add", "-s", "user", name, "--",
                             *_claude_command(name))
        if not ok:
            logger.error("Could not register the channel with Claude Code as "
                         "%s: %s", name, detail)
            return False
    # Optional and deliberately not part of the return value. This toggle was a
    # Claude channel before Codex gained the restricted board helper, and a
    # missing/broken Codex CLI must not roll that working registration back.
    _install_codex()
    _record_install(signature)
    logger.info("Channel installed: %s", launch_command())
    return True


def uninstall() -> None:
    """Unregister and remove the script.

    A session already running keeps its own copy alive until it exits: there is
    no way to unspawn an MCP server, and killing it would only make that session
    log a dead one.
    """
    for name in SERVER_NAMES:
        _claude("mcp", "remove", "-s", "user", name)
    _uninstall_codex()
    try:
        CHANNEL_SCRIPT.unlink()
    except OSError:
        pass
    try:
        _marker_path().unlink()
    except OSError:
        pass


def installed() -> bool:
    """Registered *and* present. Either half alone is a broken install — a
    registration pointing at a deleted script fails at every session start.
    Both names must be listed: during the dual-name window one without the
    other is half an install."""
    if not CHANNEL_SCRIPT.exists():
        return False
    ok, out = _claude("mcp", "list")
    return ok and all(_listed(name, out) for name in SERVER_NAMES)


def _listed(name: str, listing: str) -> bool:
    """Whether `claude mcp list` names `name` as a server of its own.

    Matched at the start of a line, `<name>:` — a bare substring test would
    find `bob` inside any other line and call a half install whole.
    """
    return any(line.strip().startswith(f"{name}:")
               for line in str(listing or "").splitlines())
