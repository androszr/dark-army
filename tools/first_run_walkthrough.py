#!/usr/bin/env python3
"""Replay a newcomer's first launch of Dark Army in a throwaway home.

The first-run checklist (`docs/first-run-checklist.md`) promises three ticks
— Enrol a folder, Open it in VS Code, Start a session — each observed by the
daemon, never pressed. The walk that found the gaps between that promise and
a fresh build (23 Sep 2026, `docs/2026-09-12-first-run-walkthrough.md` §7)
was done by hand. This is that walk as a re-runnable check:

1. install what a first launch installs — the login item (launchd stubbed),
   the hook handler and the hook groups — into a temporary ``HOME``;
2. start a headless daemon on spare ports, inside this process;
3. ``git init`` a project and enrol it through the loopback API, then check
   the key folder enrolment wrote: ``.dark-army/`` only, never the old
   ``.bob-companion/``;
4. read ``/api/state``'s ``enrollment.checklist`` for that project: nothing
   observed yet (false/false);
5. write the lock the editor extension writes when a VS Code window opens
   the folder: the editor is observed (true/false);
6. fire a ``SessionStart`` through the *installed* hook script under the
   system Python, as Claude Code would: the session is observed
   (true/true).

    cd host && .venv/bin/python ../tools/first_run_walkthrough.py \\
        --home /tmp/da-walk-$$ --api-port 29874 --hook-port 29873

It prints the evidence as one JSON line and a last line ``VERDICT: PASS`` or
``VERDICT: FAIL`` (exit 0 / 1). A refusal before anything ran exits 2.

**It touches nothing in the real home.** ``HOME`` and the three port
variables are set *before* ``dark_army_daemon`` is imported — every state
path is derived from ``Path.home()`` at import — and after the import every
home-derived constant is checked to live under the throwaway home; a process
that had already imported the modules (a test runner, say) is refused rather
than allowed to write the real ``~/.claude/settings.json``. The CLI refuses a
``--home`` equal to or above the real home and any port in 19873–19876 (the
real daemon's) before importing anything. Four things that would reach past
the home are stubbed, by name: ``launchctl`` (``enable()`` boots out the
real login item's label), the VS Code CLI, the pty broker (it would outlive
this process by design) and the ``claude agents --json`` poller. Three daemon
actions that could reach a real session are stubbed too — terminal titles,
``/compact`` and the ``claude -p`` title namer — because the hook script's
pid walk finds whatever assistant is running this tool.

Stdlib only on this side; loopback HTTP with ``X-Bob-Token`` and no
``Origin``, and the hook script over the hook socket from a separate
``/usr/bin/python3`` process. The pytest twin is
``host/tests/test_first_run_walkthrough.py``, which runs this file as a
subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parents[1] / "host"

#: The real daemon's four ports: hook bridge, loopback API, LAN door, relay.
REAL_PORTS = range(19873, 19877)

#: Every poll is bounded; a miss is a FAIL naming the step. Each returns
#: the moment its condition holds, so the bounds cost nothing on a healthy
#: machine — they are sized for a Mac running a whole fleet's test suites,
#: where one `/usr/bin/python3` start was measured past 30 s.
POLL_SECONDS = 60.0
STARTUP_SECONDS = 60.0
NOTIFY_SECONDS = 120.0

#: The variables a hook child must not inherit from this process: each one
#: would move its address or its identity (`test_notify_script._run_with_env`).
HOOK_ENV_CLEARED = ("GROK_SESSION_ID", "BOB_COMPANION_ORIGIN",
                    "DARK_ARMY_HOOK_SOCKET", "BOB_COMPANION_PORT",
                    "CLAWD_TANK_PORT")


class Refused(Exception):
    """A precondition failed before anything ran; the CLI exits 2."""


def real_home() -> Path:
    """The account's home from the password database — not `HOME`, which this
    tool itself rewrites and a test runner may already have moved."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _homes() -> list:
    """Every folder that is somebody's real home here: the password
    database's, and `$HOME` where it names a different one."""
    homes = [real_home()]
    env_home = os.environ.get("HOME", "")
    if env_home:
        resolved = Path(env_home).resolve()
        if resolved not in homes:
            homes.append(resolved)
    return homes


def check_home(home) -> Path:
    """The throwaway home, absolute, or `Refused`. Equal to or above the real
    home (the password database's, or `$HOME` where that differs) is
    refused, and so is a folder that already holds something: the CLI
    removes the home when it is done."""
    path = Path(os.path.abspath(os.path.expanduser(str(home))))
    resolved = path.resolve()
    for mine in _homes():
        if resolved == mine or resolved in mine.parents:
            raise Refused(f"--home {path} is the real home ({mine}) or contains it; "
                          "use a throwaway folder such as /tmp/da-walk-$$")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise Refused(f"--home {path} already exists and is not an empty folder")
    return path


def check_hook_socket(hook_socket, home: Path):
    """A `--hook-socket` path, absolute, or `Refused` — or None when none
    was named. The daemon clears a stale socket at its path before binding,
    so a path that already exists (the real `~/.dark-army/hook.sock`, or any
    file) is refused, and so is one under a real home; it must lie under the
    throwaway home or `/tmp`."""
    if not hook_socket:
        return None
    path = Path(os.path.abspath(os.path.expanduser(str(hook_socket))))
    if os.path.lexists(path):
        raise Refused(f"--hook-socket {path} already exists; name a new path "
                      "under the throwaway home or /tmp")
    resolved = Path(os.path.realpath(str(path)))
    for mine in _homes():
        if _under(resolved, mine):
            raise Refused(f"--hook-socket {path} lies under the real home ({mine})")
    if not (_under(resolved, Path(home)) or _under(resolved, Path("/tmp"))):
        raise Refused(f"--hook-socket {path} must lie under the throwaway home or /tmp")
    return path


def check_ports(*ports) -> None:
    for port in ports:
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise Refused(f"port {port!r} is not a usable TCP port (1024–65535)")
        if port in REAL_PORTS:
            raise Refused(f"port {port} is the real Dark Army daemon's "
                          f"({REAL_PORTS.start}–{REAL_PORTS.stop - 1}); pick a spare one")
    if len(set(ports)) != len(ports):
        raise Refused(f"ports must differ: {ports}")


def check_python(notify_python: str) -> str:
    """`notify_python --version`, or `Refused` when it cannot run at all —
    on a Mac without the Command Line Tools `/usr/bin/python3` offers to
    install them instead."""
    try:
        done = subprocess.run([notify_python, "-c", "pass"], capture_output=True,
                              timeout=30)
        if done.returncode != 0:
            raise Refused(f"{notify_python} -c pass exited {done.returncode}")
        version = subprocess.run([notify_python, "--version"], capture_output=True,
                                 text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"{notify_python} cannot run: {exc}") from exc
    return (version.stdout or version.stderr).strip()


def free_port() -> int:
    """A loopback port nobody is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _under(child, parent: Path) -> bool:
    child = Path(os.path.realpath(str(child)))
    parent = Path(os.path.realpath(str(parent)))
    return child == parent or parent in child.parents


# ── loopback HTTP ─────────────────────────────────────────────────────────────

# No proxy: a machine-wide http_proxy must not carry a loopback request away.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(port: int, method: str, path: str, token: str = "", body=None,
          timeout: float = 5.0):
    """(status, parsed JSON or None). Writes send `X-Bob-Token` and no
    `Origin`; `Authorization: Bearer` would 403 silently."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Bob-Token", token)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return status, None


def _poll(predicate, seconds: float, interval: float = 0.25):
    """Call `predicate` until it returns something truthy or time runs out;
    returns the last value either way."""
    deadline = time.monotonic() + seconds
    value = None
    while True:
        try:
            value = predicate()
        except (OSError, urllib.error.URLError, ValueError):
            value = None
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


# ── the walk ─────────────────────────────────────────────────────────────────

def walk(home, *, api_port: int, hook_port: int, hook_socket=None,
         notify_python: str = "/usr/bin/python3", refresh_seconds=None,
         log=print) -> dict:
    """Run the replay in this process and return the evidence.

    Must run in a process that has **not** imported `dark_army_daemon` or
    `dark_army_menubar` yet: the environment is set here, then they are
    imported, then every home-derived constant is verified to live under
    `home` — anything else raises `Refused` before a byte is written.
    `evidence["verdict"]` is "PASS" or "FAIL", and `evidence["failures"]`
    names each missed step.
    """
    # Re-checked here, not only in the CLI: a programmatic caller must not be
    # able to point this at a real home or a live socket either.
    home = check_home(home)
    hook_socket = check_hook_socket(hook_socket, home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lan_port = free_port()
    check_ports(api_port, hook_port, lan_port)
    python_version = check_python(notify_python)

    # ── 1. the environment, then the imports ────────────────────────────────
    os.environ["HOME"] = str(home)
    os.environ["BOB_COMPANION_API_PORT"] = str(api_port)
    os.environ["BOB_COMPANION_PORT"] = str(hook_port)
    os.environ["BOB_COMPANION_LAN_PORT"] = str(lan_port)
    os.environ.pop("CLAWD_TANK_PORT", None)
    # `paths._home()` answers a temp folder while this is set, which would
    # put the daemon's state somewhere the hook child's HOME does not point.
    # Safe to drop: the checks below hold every path under `home` instead.
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    if hook_socket:
        os.environ["DARK_ARMY_HOOK_SOCKET"] = str(hook_socket)
    else:
        os.environ.pop("DARK_ARMY_HOOK_SOCKET", None)
    if str(HOST_DIR) not in sys.path:
        sys.path.insert(0, str(HOST_DIR))

    from dark_army_daemon import (agents_poll, api_server, daemon, paths,
                                  ptyhost, socket_server, workspace)
    from dark_army_menubar import first_run, hooks, launchd, vscode_extension

    state_dir = Path(paths.STATE_DIR)
    placed = {
        "state folder": state_dir,
        "Claude settings": hooks.CLAUDE_SETTINGS_PATH,
        "Codex hooks": hooks.CODEX_HOOKS_PATH,
        "Grok hooks": hooks.GROK_HOOKS_DIR,
        "login item": launchd.PLIST_PATH,
        "hook socket": Path(socket_server.HOOK_SOCK_PATH),
    }
    # A named socket may sit under /tmp (the short-path escape hatch); every
    # other path, and a default socket, must be under the throwaway home.
    stray = [f"{what} at {where}" for what, where in placed.items()
             if not (_under(where, home)
                     or (hook_socket and what == "hook socket"
                         and _under(where, Path("/tmp"))))]
    if stray or api_server.API_PORT != api_port or socket_server.HOOK_IPC_PORT != hook_port:
        raise Refused(
            "Dark Army's modules were imported before HOME was set, so they "
            "point outside the throwaway home (" + "; ".join(stray or ["ports"])
            + "); run this tool in a fresh process")

    # ── the stubs, named ────────────────────────────────────────────────────
    def launchctl_stand_in(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=_args, returncode=0,
                                           stdout=b"", stderr=b"")
    launchd.subprocess = types.SimpleNamespace(
        run=launchctl_stand_in, SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired)

    def no_vscode_cli(*_args, **_kwargs):
        raise OSError("the replay never runs the VS Code CLI")
    vscode_extension.run_code = no_vscode_cli

    async def no_broker(self):
        return False
    ptyhost.PtyHost.attach = no_broker
    agents_poll.AgentsPoller.start = lambda self: None

    evidence: dict = {
        "home": str(home), "state_dir": str(state_dir),
        "api_port": api_port, "hook_port": hook_port,
        "hook_socket": str(hook_socket or socket_server.HOOK_SOCK_PATH),
        "notify_python": notify_python,
        "notify_python_version": python_version,
        "facts": [], "failures": [],
    }
    failures = evidence["failures"]

    # ── what a first launch installs ────────────────────────────────────────
    paths.ensure_state_dir()
    report = first_run.apply_first_run()
    hooks.install_notify_script()
    hooks_ok = hooks.install_hooks()
    evidence["first_run_applied"] = bool(report.applied)
    evidence["plist_present"] = launchd.PLIST_PATH.exists()
    evidence["notify_script_present"] = paths.NOTIFY_SCRIPT_PATH.is_file()
    try:
        settings = json.loads(hooks.CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    groups = sorted(
        event for event, entries in (settings.get("hooks") or {}).items()
        if "dark-army-notify" in json.dumps(entries))
    evidence["settings_hook_events"] = groups
    if not (hooks_ok and evidence["plist_present"] and evidence["notify_script_present"]
            and "SessionStart" in groups):
        failures.append("install: login item, hook script or hook groups missing")

    # ── 2. the daemon, on its own thread ────────────────────────────────────
    if refresh_seconds:
        daemon.SNAPSHOT_REFRESH_SECONDS = float(refresh_seconds)
        workspace.CACHE_TTL_SECONDS = float(refresh_seconds)
    d = daemon.BobDaemon(headless=True)
    if refresh_seconds:
        # The full tier is what rebuilds the checklist facts; its floor is
        # a replay cadence knob here, like the refresh tick above.
        d.FULL_PUSH_INTERVAL_SECONDS = float(refresh_seconds)
    # Signal handlers need the main thread, and the replay stops the daemon
    # itself. The three daemon actions that could reach a real session:
    d._stop_on_signals = lambda: None
    d._apply_terminal_titles = lambda *a, **k: None

    async def nothing(*_a, **_k):
        return None
    d._flush_session_titles = nothing
    d._flush_auto_compacts = nothing

    thread = threading.Thread(target=lambda: asyncio.run(d.run()),
                              name="replay-daemon", daemon=True)
    thread.start()
    try:
        _walk_steps(evidence, home=home, api_port=api_port,
                    notify_python=notify_python, hook_socket=hook_socket,
                    token_path=Path(api_server.API_TOKEN_PATH),
                    # The desk token, off the in-process daemon: it is never
                    # on disk, and `enroll_project` is a desk verb.
                    desk_token=d.desk_token,
                    ide_dir=state_dir / "ide",
                    script=Path(paths.NOTIFY_SCRIPT_PATH), log=log)
    finally:
        loop = getattr(d, "_loop", None)
        if loop is not None and thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(d._shutdown(), loop).result(15)
            except Exception as exc:  # the verdict is already decided; say so
                log(f"daemon shutdown: {exc!r}")
        thread.join(15)
        evidence["daemon_stopped"] = not thread.is_alive()

    evidence["verdict"] = "FAIL" if failures else "PASS"
    return evidence


def _walk_steps(evidence: dict, *, home: Path, api_port: int, notify_python: str,
                hook_socket, token_path: Path, desk_token, ide_dir: Path,
                script: Path, log) -> None:
    failures = evidence["failures"]

    # The session token's file is still created, and private; the desk token
    # the enrolment needs comes off the daemon in this process, once
    # `/api/state` answers — it is never written anywhere to be read back.
    session = _poll(lambda: token_path.is_file() and token_path.read_text().strip(),
                    STARTUP_SECONDS)
    up = session and _poll(lambda: _http(api_port, "GET", "/api/state")[0] == 200,
                           STARTUP_SECONDS)
    if not up:
        failures.append("step 2: the daemon's API never answered /api/state")
        return
    token = desk_token()
    evidence["session_token_mode"] = oct(token_path.stat().st_mode & 0o777)
    evidence["desk_token_on_disk"] = bool(token) and token in token_path.read_text()
    if not token or evidence["desk_token_on_disk"]:
        failures.append("step 2: the daemon has no desk token in memory, "
                        "or it is on disk")
        return
    log(f"daemon up on 127.0.0.1:{api_port}")

    # ── 3. enrol a project ──────────────────────────────────────────────────
    proj = home / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=False,
                   capture_output=True, timeout=30)
    status, answer = _http(api_port, "POST", "/api/action", token,
                           {"action": "enroll_project", "root": str(proj)})
    evidence["enrol"] = {"status": status, "ok": bool((answer or {}).get("ok"))}
    key_dirs = sorted(p.name for p in proj.iterdir()
                      if p.is_dir() and (p / "key").is_file())
    evidence["key_dir"] = key_dirs[0] if len(key_dirs) == 1 else key_dirs
    key = proj / ".dark-army" / "key"
    evidence["key_mode"] = oct(key.stat().st_mode & 0o777) if key.exists() else ""
    ignore = proj / ".dark-army" / ".gitignore"
    evidence["key_dir_gitignore"] = ignore.read_text() if ignore.exists() else ""
    project_ignore = proj / ".gitignore"
    evidence["project_gitignore_lines"] = (
        project_ignore.read_text().splitlines().count(".dark-army/")
        if project_ignore.exists() else 0)
    evidence["legacy_key_dir_present"] = (proj / ".bob-companion").exists()
    evidence["project_entries"] = sorted(p.name for p in proj.iterdir()
                                         if p.name != ".git")
    if not (status == 200 and evidence["enrol"]["ok"]
            and evidence["key_dir"] == ".dark-army"
            and evidence["key_mode"] == "0o600"
            and evidence["key_dir_gitignore"] == "*\n"
            and evidence["project_gitignore_lines"] == 1
            and not evidence["legacy_key_dir_present"]):
        failures.append("step 3: enrolment did not write exactly .dark-army/")
        return
    log(f"enrolled {proj}")

    root = os.path.realpath(str(proj))
    evidence["project"] = root

    def facts():
        status, state = _http(api_port, "GET", "/api/state", timeout=10.0)
        if status != 200 or not isinstance(state, dict):
            return None
        roots = ((state.get("enrollment") or {}).get("checklist") or {}).get("roots") or []
        return next((r for r in roots if r.get("root") == root), None)

    def reach(step: str, want: tuple) -> bool:
        seen = {}

        def matches():
            fact = facts()
            if fact is not None:
                seen.update(fact)
            return fact is not None and (
                fact.get("editor_observed"), fact.get("session_observed")) == want
        _poll(matches, POLL_SECONDS)
        got = [bool(seen.get("editor_observed")), bool(seen.get("session_observed"))]
        evidence["facts"].append(got)
        if "own_checkout" in seen:
            evidence["own_checkout"] = bool(seen["own_checkout"])
        if tuple(got) != want or not seen:
            failures.append(f"{step}: expected {list(want)}, saw "
                            f"{got if seen else 'no facts for the project'}")
            return False
        log(f"{step}: {got}")
        return True

    # ── 4. nothing observed yet ─────────────────────────────────────────────
    if not reach("step 4 (enrolled)", (False, False)):
        return
    if evidence.get("own_checkout") is not False:
        failures.append("step 4: the project was marked as Dark Army's own checkout")

    # ── 5. the editor extension's lock ──────────────────────────────────────
    ide_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_port = free_port()
    lock = ide_dir / f"{lock_port}.lock"
    body = json.dumps({
        "port": lock_port, "pid": os.getpid(), "extHostPid": os.getpid(),
        "workspaceFolders": [str(proj)], "workspaceName": proj.name,
        "workspaceUri": None, "workspaceFsPath": None,
        "ideName": "replay", "authToken": str(uuid.uuid4()),
        "transport": "http", "extensionVersion": _extension_version(),
        "extensionId": "dark-army.dark-army-ide",
        "startedAt": int(time.time()),
    })
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(body)
    if not reach("step 5 (editor lock)", (True, False)):
        return

    # ── 6. a session through the installed hook script ─────────────────────
    env = dict(os.environ, HOME=str(home))
    for name in HOOK_ENV_CLEARED:
        env.pop(name, None)
    if hook_socket:
        env["DARK_ARMY_HOOK_SOCKET"] = str(hook_socket)
    payload = {"hook_event_name": "SessionStart", "session_id": "walk-1",
               "cwd": str(proj),
               "transcript_path": str(home / ".claude" / "projects" / "x" / "walk-1.jsonl")}
    try:
        done = subprocess.run([notify_python, str(script)], cwd=str(proj), env=env,
                              input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, timeout=NOTIFY_SECONDS)
        evidence["notify_exit"] = done.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        evidence["notify_exit"] = None
        failures.append(f"step 6: the hook script did not run: {exc}")
        return
    if done.returncode != 0:
        failures.append(f"step 6: the hook script exited {done.returncode}: "
                        + done.stderr.decode("utf-8", "replace")[-300:])
        return
    reach("step 6 (session)", (True, True))


def _extension_version() -> str:
    try:
        manifest = HOST_DIR.parent / "vscode-extension" / "package.json"
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "0.0.0"


# ── the CLI ──────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--home", required=True,
                        help="a throwaway home (absent or empty); short, e.g. /tmp/da-walk-$$")
    parser.add_argument("--api-port", type=int, required=True)
    parser.add_argument("--hook-port", type=int, required=True)
    parser.add_argument("--hook-socket", default=None,
                        help="name the hook socket for daemon and script alike "
                             "(default: <home>/.dark-army/hook.sock)")
    parser.add_argument("--notify-python", default="/usr/bin/python3")
    parser.add_argument("--refresh-seconds", type=float, default=None)
    parser.add_argument("--keep", action="store_true",
                        help="leave the throwaway home in place afterwards")
    args = parser.parse_args(argv)

    # Every refusal comes before any `dark_army_*` import.
    try:
        home = check_home(args.home)
        check_ports(args.api_port, args.hook_port)
        check_hook_socket(args.hook_socket, home)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    def log(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        evidence = walk(home, api_port=args.api_port, hook_port=args.hook_port,
                        hook_socket=args.hook_socket,
                        notify_python=args.notify_python,
                        refresh_seconds=args.refresh_seconds, log=log)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
        return 2
    if not args.keep:
        shutil.rmtree(home, ignore_errors=True)
    print(json.dumps(evidence, sort_keys=True), flush=True)
    print(f"VERDICT: {evidence['verdict']}", flush=True)
    return 0 if evidence["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
