# host/dark_army_menubar/statusline.py
"""Install the Claude Code statusline collector.

Claude Code runs the configured `statusLine` command on every conversation update
and pipes it a JSON blob describing the session. That blob is the only place Dark Army
can learn three things it otherwise has to guess or do without:

  * ``cost.total_cost_usd`` — the session's real cost. Transcripts carry no cost
    field at all, so the alternative is a hand-maintained pricing table.
  * ``context_window.used_percentage`` — how close the session is to compacting.
  * ``rate_limits.{five_hour,seven_day}`` — how much of the budget is spent.

Only one statusLine can be configured, so installing Dark Army's would silently take
away a status line the user built. It doesn't: the displaced command is saved to
``statusline-chain`` and our script runs it and prints its output verbatim.
"""

import json
import logging
import textwrap

from dark_army_daemon.paths import (
    STATUSLINE_CHAIN_PATH,
    STATUSLINE_SCRIPT_PATH,
    ensure_state_dir,
)
from .hooks import (
    CLAUDE_SETTINGS_PATH,
    load_claude_settings,
    read_claude_settings,
    script_current,
    write_claude_settings,
    write_script_atomic,
)

logger = logging.getLogger("dark-army.statusline")

SCRIPT_VERSION = "2026-09-23-private-hook-socket"

# Standalone: runs under whatever python3 Claude Code finds, so stdlib only.
STATUSLINE_SCRIPT = textwrap.dedent('''\
    #!/usr/bin/env python3
    """dark-army-statusline - Claude Code statusline collector for Dark Army.

    Forwards the session payload to the daemon verbatim. All knowledge of the
    payload's shape lives in dark_army_daemon/protocol.flatten_statusline, so
    unlike the notify script there is no second copy of the contract to drift.
    """
    # STATUSLINE_SCRIPT_VERSION: {version}

    import json
    import os
    import socket
    import subprocess
    import sys
    from pathlib import Path

    # The one address rule, the notify script's: a socket path named in the
    # environment, else a port named there (the bridge, on its way out), else
    # the private socket in Dark Army's own folder — and never a second try.
    HOOK_SOCKET = os.environ.get("DARK_ARMY_HOOK_SOCKET", "")
    HOOK_PORT = os.environ.get("BOB_COMPANION_PORT") or os.environ.get("CLAWD_TANK_PORT", "")
    DEFAULT_HOOK_SOCKET = os.path.join(os.path.expanduser("~"), ".dark-army", "hook.sock")
    CHAIN_PATH = Path.home() / ".dark-army" / "statusline-chain"

    # Claude Code cancels an in-flight statusline as soon as the next update
    # arrives, so every wait here is deliberately short: a slow status line is a
    # blank one. A daemon that isn't running refuses the connection immediately.
    SEND_TIMEOUT = 0.4
    CHAIN_TIMEOUT = 2.0

    # Dark Army only watches projects you enrolled, and the key that says so lives in
    # a file inside the project. The same read as the notify script's, for the
    # same reason: an unkeyed collector is refused, and must not learn how many
    # *other* agents are waiting — that would be a cross-project leak out of a
    # project Dark Army was never told to watch.
    #
    # The key lives in `<root>/.dark-army/key`; a project enrolled before the
    # move may hold only `<root>/.bob-companion/key`, read second while the
    # read window is open.
    #
    # The walk-up skips the home directory on purpose, under both names:
    # `~/.dark-army` is the state folder and `~/.bob-companion` a link to it,
    # both the names a project's key folder has, so without the skip any
    # session under $HOME would enrol the whole home directory. The skip is
    # per directory, so it covers every name.
    KEY_DIR_NAMES = (".dark-army", ".bob-companion")
    KEY_FILE_NAME = "key"
    MAX_KEY_BYTES = 512
    MAX_WALK_DEPTH = 40


    def read_key(directory):
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


    def project_key(cwd):
        """The enrolment key of the project `cwd` sits in, or ""."""
        try:
            start = os.path.realpath(cwd or os.getcwd())
        except OSError:
            return ""
        try:
            home = os.path.realpath(str(Path.home()))
        except OSError:
            home = ""
        here = start
        for _ in range(MAX_WALK_DEPTH):
            if not here:
                break
            if here != home:
                found = read_key(here)
                if found:
                    return found
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
        return ""


    def payload_cwd(payload):
        workspace = payload.get("workspace") or {{}}
        for key in ("current_dir", "project_dir"):
            value = workspace.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""


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


    def report(payload):
        """Forward to the daemon and return its reply ({{}} if it is not running)."""
        msg = {{
            "event": "statusline",
            "session_id": payload.get("session_id", ""),
            "key": project_key(payload_cwd(payload)),
            "data": payload,
        }}
        try:
            with _hook_connect(SEND_TIMEOUT) as sock:
                sock.sendall(json.dumps(msg).encode("utf-8") + b"\\n")
                sock.settimeout(SEND_TIMEOUT)
                reply = sock.makefile("r", encoding="utf-8").readline()
            return json.loads(reply) if reply.strip() else {{}}
        except (OSError, ValueError):
            return {{}}


    # A run of this collector started by a chain (however indirectly) sees
    # this set and never chains again: one level, whatever the chain file
    # says. After a downgrade the old app can record the new collector as
    # "the user's", and the old name is a link to the new file.
    DEPTH_ENV = "DARK_ARMY_STATUSLINE_DEPTH"


    def _our_paths():
        """This collector's real path under every name it has."""
        home = Path.home()
        out = set()
        for p in (globals().get("__file__") or "",
                  home / ".dark-army" / "dark-army-statusline"):
            if p:
                try:
                    out.add(os.path.realpath(str(p)))
                except (OSError, ValueError):
                    pass
        return out


    def names_ourselves(command):
        """True when any word of `command` resolves to this collector. Such a
        chain would only run us again."""
        import shlex
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        ours = _our_paths()
        for word in words:
            try:
                if os.path.realpath(os.path.expanduser(word)) in ours:
                    return True
            except (OSError, ValueError):
                continue
        return False


    def chained(raw):
        """Run the statusline command Dark Army displaced, if any. None if there is none
        or it failed — in which case we fall back to our own line rather than
        printing nothing. Never a chain that leads back here."""
        if os.environ.get(DEPTH_ENV):
            return None
        try:
            command = CHAIN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not command or names_ourselves(command):
            return None
        env = dict(os.environ)
        env[DEPTH_ENV] = "1"
        try:
            done = subprocess.run(
                command, shell=True, input=raw, capture_output=True,
                text=True, timeout=CHAIN_TIMEOUT, env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return done.stdout.rstrip("\\n") if done.returncode == 0 else None


    def pretty_model(payload):
        name = ((payload.get("model") or {{}}).get("display_name") or "").strip()
        if name.startswith("claude-"):
            name = name[len("claude-"):]
        return name.replace("-", " ") or "?"


    def own_line(payload, reply):
        ctx = payload.get("context_window") or {{}}
        cost = payload.get("cost") or {{}}
        parts = ["\\U0001F438 " + pretty_model(payload)]
        pct = ctx.get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append("ctx {{}}%".format(int(pct)))
        usd = cost.get("total_cost_usd")
        if isinstance(usd, (int, float)):
            parts.append("${{:.2f}}".format(usd))
        line = "  \\u00b7  ".join(parts)

        # The point of the exercise: the agent you are looking at tells you that a
        # different one is stuck waiting for you.
        waiting = reply.get("others_waiting")
        if isinstance(waiting, int) and waiting > 0:
            line += "   \\u26a0 {{}} agent{{}} waiting".format(
                waiting, "" if waiting == 1 else "s"
            )
        return line


    def main():
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {{}}

        reply = report(payload) if payload else {{}}
        passthrough = chained(raw)
        print(passthrough if passthrough is not None else own_line(payload, reply))


    if __name__ == "__main__":
        main()
''').format(version=SCRIPT_VERSION)

STATUSLINE_COMMAND = str(STATUSLINE_SCRIPT_PATH)


def _is_command(command, path: str) -> bool:
    return command == path or command.startswith(path + " ")


def _command_is_current(command) -> bool:
    """Our exact current script path, optionally with args."""
    return isinstance(command, str) and _is_command(command, STATUSLINE_COMMAND)


def _command_is_ours(command) -> bool:
    """Our exact script path, optionally with args. A command that merely
    contains the path (a wrapper, `cat <path>`) belongs to the user."""
    if not isinstance(command, str):
        return False
    return _is_command(command, STATUSLINE_COMMAND)


def _chain_names_ours(command: str) -> bool:
    """True when a recorded chain command would run our collector, by any of
    its words."""
    import os
    import shlex
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    ours = {os.path.realpath(STATUSLINE_COMMAND), STATUSLINE_COMMAND}
    for word in words:
        if word in ours or os.path.realpath(os.path.expanduser(word)) in ours:
            return True
    return False


def _read_chain() -> str:
    try:
        return STATUSLINE_CHAIN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _drop_self_chain() -> None:
    """Delete a chain file that names our own collector: it is never the
    person's status line, and run it would only call us back."""
    command = _read_chain()
    if command and _chain_names_ours(command):
        STATUSLINE_CHAIN_PATH.unlink(missing_ok=True)
        logger.info("Removed a status-line chain that named Dark Army's own collector")


def install_statusline_script() -> None:
    """Write the collector — skipped when the installed copy is already
    byte-for-byte current, and replaced atomically otherwise. Claude Code runs
    this file on every conversation update, so a truncating write on the live
    path was a collector that could execute half-written."""
    ensure_state_dir()
    if script_current(STATUSLINE_SCRIPT_PATH, STATUSLINE_SCRIPT):
        return
    write_script_atomic(STATUSLINE_SCRIPT_PATH, STATUSLINE_SCRIPT)
    logger.info("Installed statusline collector: %s", STATUSLINE_SCRIPT_PATH)


def installed_script_version() -> str:
    """Version marker of the script on disk, or "" if absent/unreadable."""
    try:
        for line in STATUSLINE_SCRIPT_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("# STATUSLINE_SCRIPT_VERSION:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def is_statusline_installed() -> bool:
    settings = load_claude_settings()
    entry = settings.get("statusLine")
    return isinstance(entry, dict) and _command_is_current(entry.get("command"))


def install_statusline() -> bool:
    """Point Claude Code's statusLine at our collector, preserving whatever was
    there. Idempotent: reinstalling does not chain our own script to itself, and
    does not lose a chain captured by an earlier install."""
    install_statusline_script()
    _drop_self_chain()
    ok, settings = read_claude_settings(for_write=True)
    if not ok:
        # Same rule as the hooks and title-env writers: {} is not the user's
        # settings, and writing it back would discard the whole file to install
        # one key.
        logger.warning("settings.json is not readable; not installing statusLine")
        return False
    existing = settings.get("statusLine")

    if isinstance(existing, dict) and not _command_is_ours(existing.get("command")):
        command = existing.get("command")
        if (existing.get("type") == "command" and isinstance(command, str)
                and command.strip() and not _command_is_ours(command.strip())
                and not _chain_names_ours(command.strip())):
            ensure_state_dir()
            STATUSLINE_CHAIN_PATH.write_text(command.strip() + "\n", encoding="utf-8")
            logger.info("Chained the existing statusline command: %s", command)
        else:
            # A statusLine we cannot re-run (unknown type). Replacing it would be
            # silent data loss, so record it verbatim next to the chain file and
            # say so — the user can restore it by hand.
            logger.warning(
                "Replacing an unsupported statusLine entry; original was %r",
                existing,
            )

    settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
    write_claude_settings(settings)
    logger.info("Installed statusLine in %s", CLAUDE_SETTINGS_PATH)
    return True


def uninstall_statusline() -> bool:
    """Remove our statusLine, restoring the user's if we displaced one."""
    ok, settings = read_claude_settings(for_write=True)
    if not ok:
        logger.warning("settings.json is not readable; not removing statusLine")
        return False
    entry = settings.get("statusLine")
    if not (isinstance(entry, dict) and _command_is_ours(entry.get("command"))):
        return False  # not ours; leave it alone

    chained = _read_chain()
    if chained and _chain_names_ours(chained):
        # Never "restore" our own collector as the person's.
        chained = ""

    if chained:
        settings["statusLine"] = {"type": "command", "command": chained}
    else:
        settings.pop("statusLine", None)
    write_claude_settings(settings)

    STATUSLINE_CHAIN_PATH.unlink(missing_ok=True)
    logger.info("Removed statusLine%s", " (restored the previous one)" if chained else "")
    return True
