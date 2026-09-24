#!/usr/bin/env python3
"""exempt - open or close this session's window past the shunt guard.

    python3 .claude/skills/shunt/exempt.py on | off | status

Part of the shunt skill (SKILL.md beside this file). Stdlib only, 3.9-safe,
no Dark Army import.

`on` creates `~/.dark-army/shunt-exempt/<session id>` for every session
id this shell's environment exposes; the guard leaves a session with a
marker alone, which is how the ship workflow lets a reviewer read any file
whole. `off` removes them and prunes markers older than a day, so a window
nobody closed cannot outlive its run. `status` says which ids are open.

Exit 0 when no id is found: a session Dark Army does not know is never
blocked in the first place, so there is nothing to open.
"""
import os
import sys
import time

SESSION_ID_KEYS = ("CLAUDE_CODE_SESSION_ID", "GROK_SESSION_ID",
                   "CODEX_THREAD_ID", "CODEX_SESSION_ID")
STALE_SECONDS = 24 * 3600


def _state_home(env):
    """Dark Army's state folder: `~/.dark-army` where it exists, else the
    `~/.bob-companion` of a Mac still on the old install, else `~/.dark-army`.
    Never creates either: a wrapper run before the app has moved the folder
    must not make the new one early."""
    home = env.get("HOME") or os.path.expanduser("~")
    new = os.path.join(home, ".dark-army")
    if os.path.isdir(new):
        return new
    old = os.path.join(home, ".bob-companion")
    if os.path.isdir(old):
        return old
    return new


def exempt_dir(env):
    return os.path.join(_state_home(env), "shunt-exempt")


def session_ids(env):
    """Every id the environment exposes, in a fixed order, de-duplicated.
    A marker is written for each: Grok and Codex sub-agents share the
    parent's id in the hook input, which is why the window is keyed by
    session and not by agent."""
    out = []
    for key in SESSION_ID_KEYS:
        value = str(env.get(key) or "").strip()
        if value and "/" not in value and value not in (".", "..") and value not in out:
            out.append(value)
    return out


def marker_path(env, sid):
    return os.path.join(exempt_dir(env), sid)


def turn_on(env):
    ids = session_ids(env)
    if not ids:
        print("exempt: no session id in the environment; nothing to open")
        return 0
    folder = exempt_dir(env)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    for sid in ids:
        path = marker_path(env, sid)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.utime(path, None)
    print("exempt: on for " + ", ".join(ids))
    return 0


def prune(env):
    folder = exempt_dir(env)
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    now = time.time()
    dropped = 0
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and now - os.stat(path).st_mtime > STALE_SECONDS:
                os.unlink(path)
                dropped += 1
        except OSError:
            continue
    return dropped


def turn_off(env):
    ids = session_ids(env)
    closed = []
    for sid in ids:
        try:
            os.unlink(marker_path(env, sid))
            closed.append(sid)
        except OSError:
            continue
    stale = prune(env)
    if closed:
        print("exempt: off for " + ", ".join(closed))
    elif ids:
        print("exempt: already off")
    else:
        print("exempt: no session id in the environment; nothing to close")
    if stale:
        print("exempt: pruned %d marker(s) older than a day" % stale)
    return 0


def status(env):
    ids = session_ids(env)
    if not ids:
        print("exempt: no session id in the environment")
        return 0
    for sid in ids:
        state = "on" if os.path.isfile(marker_path(env, sid)) else "off"
        print("exempt: %s %s" % (state, sid))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    verb = argv[0] if argv else ""
    env = dict(os.environ)
    if verb == "on":
        return turn_on(env)
    if verb == "off":
        return turn_off(env)
    if verb == "status":
        return status(env)
    print("usage: exempt.py on | off | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
