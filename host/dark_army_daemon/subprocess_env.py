"""Strip what a child Dark Army starts must never inherit: py2app's leaked Python
vars, and the identity of whatever assistant session Dark Army itself was started
from.

py2app's launcher stub setenv()s ``PYTHONHOME`` (and friends) into the running
.app, pointing at ``Contents/Resources``. Every child inherits them, so
``host/.venv/bin/python`` and ``pytest`` boot against the bundle's frozen
stdlib instead of the venv — the bundle has no pytest, and the venv's
site-packages never load. Rebuild already stripped these before ``build.sh``;
agent sessions, the Grok leader client, Prepare helpers and ``code`` CLI
spawns did not, which is how a Grok session Dark Army started could not run the
host tests.

**And a session's identity.** The menu-bar app is routinely relaunched by a
dev rebuild from *inside* an assistant's terminal, and the pty broker it
starts keeps that environment for days. An assistant started on Dark Army's own pty
then inherits ``GROK_SESSION_ID`` / ``CLAUDE_CODE_SESSION_ID`` / a stale
``BOB_COMPANION_ORIGIN`` from a session that is not it: the hook stamps it as
the wrong provider (so liveness evicts a live claude as "PID gone"), its
channel announces somebody else's session id (refused as "already held") and
its row says it was opened to plan a card it never saw. ``SESSION_ENV_VARS``
is that list; a deliberate stamp is put back by the caller *after* the strip
(`PtyHost.start`, `vscode_reveal.spawn_agent`'s merge order).
"""
from __future__ import annotations

import os
from typing import Mapping, Optional


#: Names py2app writes into the frozen app's environment. A child that should
#: run against a real interpreter (venv pytest, grok, claude, ``code``) must
#: not see them. PATH and HOME stay (PATH gains the login entries).
PY2APP_ENV_VARS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONEXECUTABLE",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONOPTIMIZE",
    "PYTHONUNBUFFERED",
    "RESOURCEPATH",
    "EXECUTABLEPATH",
    "ARGVZERO",
    "_PY2APP_LAUNCHED_",
)

#: Names that say *which session and which terminal program this process is*.
#: A child Dark Army starts is a new session on Dark Army's own terminal, so every one of
#: these is somebody else's if inherited. ``BOB_COMPANION_ORIGIN`` is here
#: too: the stamp a spawn carries is set by its caller after the strip, and
#: an inherited one names the press that started *the app*, not this child.
#: Settings a person sets stay — ``CLAUDE_CODE_DISABLE_TERMINAL_TITLE``,
#: ``CLAUDE_CONFIG_DIR``, ``ANTHROPIC_*`` — which is why this is a list and
#: a few prefixes, never ``CLAUDE_*`` whole.
SESSION_ENV_VARS = (
    "GROK_SESSION_ID",
    "GROK_AGENT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_CHILD_SESSION",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "BOB_COMPANION_ORIGIN",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
)

#: Families of per-session names Claude Code mints and renames between
#: versions (the messaging socket and token, the bridge session id, the
#: attended flag). A prefix, because the exact spelling moves.
SESSION_ENV_PREFIXES = (
    "CLAUDE_CODE_SESSION_",
    "CLAUDE_CODE_MESSAGING_",
    "CLAUDE_CODE_BRIDGE_",
)

#: Every exact name ``clean_env`` removes; the prefixes are on top.
STRIPPED_ENV_VARS = PY2APP_ENV_VARS + SESSION_ENV_VARS


def is_stripped(name: str) -> bool:
    """Whether ``clean_env`` drops ``name``: an exact member of
    ``STRIPPED_ENV_VARS`` or under one of ``SESSION_ENV_PREFIXES``."""
    return name in STRIPPED_ENV_VARS or name.startswith(SESSION_ENV_PREFIXES)


#: Where macOS keeps a login shell's PATH: `/usr/libexec/path_helper` reads
#: this file, then every file in the folder in name order, one entry a line.
PATHS_FILE = "/etc/paths"
PATHS_DIR = "/etc/paths.d"


def login_path_entries(paths_file: Optional[str] = None,
                       paths_dir: Optional[str] = None) -> list[str]:
    """The entries a login shell's `path_helper` would put on PATH, in its
    order. A file that cannot be read contributes nothing; never raises.
    The module's `PATHS_FILE` / `PATHS_DIR` are read at call time."""
    paths_file = PATHS_FILE if paths_file is None else paths_file
    paths_dir = PATHS_DIR if paths_dir is None else paths_dir
    files = [paths_file]
    try:
        files += [os.path.join(paths_dir, name)
                  for name in sorted(os.listdir(paths_dir))]
    except OSError:
        pass
    out: list[str] = []
    for name in files:
        try:
            with open(name, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            entry = line.strip()
            if entry and not entry.startswith("#") and entry not in out:
                out.append(entry)
    return out


def with_login_path(path: str, entries) -> str:
    """``path`` with every login entry it lacks appended, in order. What is
    already there keeps its place, so nothing a person put first is shadowed."""
    have = [p for p in str(path or "").split(os.pathsep) if p]
    for entry in entries:
        if entry not in have:
            have.append(entry)
    return os.pathsep.join(have)


def adopt_login_path(environ=None) -> str:
    """Give *this process* a login shell's PATH, once, at its own start.

    `clean_env` fixes the PATH of each child built through it, but only
    where the running code has it: the pty broker outlives app restarts on
    purpose, so a broker started before that fix went on handing every new
    terminal the bare launchd PATH (`node` missing on a Mac that has it,
    22 Sep 2026), and the app's own lookups (`env node` for the Codex board
    helper) never went through `clean_env` at all. Called first thing in
    the app's and the broker's `main`, every later child inherits the full
    PATH whatever builds its environment. Appends only; idempotent."""
    env = os.environ if environ is None else environ
    env["PATH"] = with_login_path(env.get("PATH", ""), login_path_entries())
    return env["PATH"]


def clean_env(base: Optional[Mapping] = None) -> dict:
    """A copy of ``base`` (or ``os.environ``) with the frozen-app Python vars
    and the inherited session identity gone, and PATH made a login shell's.

    **The PATH.** Dark Army is started by launchd like any app, so it — and
    the pty broker it starts, and every child of either — inherits the bare
    `/usr/bin:/bin:/usr/sbin:/sbin`. macOS adds `/etc/paths.d` (Homebrew's
    `/opt/homebrew/bin` among them) only in a *login* shell, and the
    assistants' shells are not one: an agent on Dark Army's own terminal
    could not find `node`, `npm`, `gh` or `brew`, and the host suite's node
    tests failed as "no node" on a Mac that has it (22 Sep 2026). The login
    entries are appended, never put first."""
    src = os.environ if base is None else base
    out = {k: v for k, v in src.items() if not is_stripped(k)}
    out["PATH"] = with_login_path(out.get("PATH", ""), login_path_entries())
    return out


def unset_payload() -> dict:
    """JSON-null map the VS Code extension uses to *delete* these from a
    spawned terminal. ``createTerminal({env})`` merges; a null value removes.
    A caller adding its own stamp merges it **after** this map. The prefixed
    families cannot be named here — a null needs an exact key — so a VS Code
    terminal keeps those; its shell is not a session's child the way Dark Army's
    pty is, and the exact names carry the identity that misroutes."""
    return {name: None for name in STRIPPED_ENV_VARS}
