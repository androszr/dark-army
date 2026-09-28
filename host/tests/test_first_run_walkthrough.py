# host/tests/test_first_run_walkthrough.py
"""The first-run replay, run by the suite: `tools/first_run_walkthrough.py`.

A newcomer's first launch, in a throwaway home with no real VS Code: the
hook script is installed, a headless daemon starts on spare ports, a folder
is enrolled, the editor extension's lock appears and a `SessionStart` goes
through the *installed* hook script under the system Python. The checklist's
two facts must go false/false → true/false → true/true, and enrolment must
have written the key folder under its Dark Army name only.

**Run as a subprocess, never in-process.** Every state path Dark Army has
(`paths.STATE_DIR`, `hooks.CLAUDE_SETTINGS_PATH`, `launchd.PLIST_PATH`, …)
is derived from `Path.home()` when the module is imported, and pytest has
imported them long before this file runs — pointed at the conftest's temp
folder at best and at the real `~/.claude/settings.json` at worst. A fresh
process sets `HOME` first; the tool refuses to run where it cannot.

The home is short and under `/tmp` (the `short_home` recipe in
`test_notify_script.py`): `<home>/.dark-army/hook.sock` must fit the
~104-byte AF_UNIX path limit, and the default socket rung is the one this
check exercises.
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.free_ports import free_port

TOOL = Path(__file__).resolve().parents[2] / "tools" / "first_run_walkthrough.py"
SYSTEM_PYTHON = "/usr/bin/python3"


def _system_python_runs() -> bool:
    try:
        return subprocess.run([SYSTEM_PYTHON, "-c", "pass"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _free_port() -> int:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_port()


def _real_ide_locks() -> dict:
    """The real home's editor locks, by name → bytes. Read from the password
    database's home: pytest may have moved `HOME`."""
    ide = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".dark-army" / "ide"
    out = {}
    for lock in ide.glob("*.lock"):
        try:
            out[lock.name] = lock.read_bytes()
        except OSError:
            pass
    return out


@pytest.fixture
def short_home():
    path = tempfile.mkdtemp(prefix="da-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.skipif(not _system_python_runs(),
                    reason=f"{SYSTEM_PYTHON} cannot run (no Command Line Tools)")
def test_a_first_launch_replay_ticks_both_facts_in_order(short_home):
    before = _real_ide_locks()
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    api_port, hook_port = _free_port(), _free_port()
    while hook_port == api_port:
        hook_port = _free_port()
    done = subprocess.run(
        [sys.executable, str(TOOL), "--home", short_home,
         "--api-port", str(api_port), "--hook-port", str(hook_port),
         "--refresh-seconds", "1"],
        # The tool's own bounds (startup, three polls, the hook script) sum
        # well under this; they return early on a healthy machine.
        env=env, capture_output=True, text=True, timeout=480)
    lines = done.stdout.strip().splitlines()
    assert lines, f"no output; stderr:\n{done.stderr[-3000:]}"
    assert lines[-1] == "VERDICT: PASS", (
        f"{done.stdout[-3000:]}\n--- stderr ---\n{done.stderr[-3000:]}")
    assert done.returncode == 0
    evidence = json.loads(next(line for line in reversed(lines) if line.startswith("{")))

    assert evidence["facts"] == [[False, False], [True, False], [True, True]]
    assert evidence["own_checkout"] is False
    assert evidence["key_dir"] == ".dark-army"
    assert evidence["legacy_key_dir_present"] is False
    assert evidence["key_mode"] == "0o600"
    # The session token's file is private and the desk token is not in it:
    # the enrolment rode the desk token off the in-process daemon.
    assert evidence["session_token_mode"] == "0o600"
    assert evidence["desk_token_on_disk"] is False
    assert evidence["key_dir_gitignore"] == "*\n"
    assert evidence["project_gitignore_lines"] == 1
    assert evidence["project_entries"] == [".dark-army", ".gitignore"]
    assert evidence["notify_python_version"].startswith("Python 3.")
    assert evidence["notify_exit"] == 0
    assert evidence["plist_present"] and evidence["notify_script_present"]
    assert "SessionStart" in evidence["settings_hook_events"]
    assert evidence["state_dir"] == os.path.join(short_home, ".dark-army")
    assert evidence["daemon_stopped"] is True

    # Nothing reached the real home: no lock written by the replay there
    # (a real VS Code window opening meanwhile is not the replay's).
    gained = {name: body for name, body in _real_ide_locks().items()
              if before.get(name) != body}
    assert not [name for name, body in gained.items()
                if b'"replay"' in body or short_home.encode() in body]
    # And the home is the tool's to remove.
    assert not os.path.exists(short_home)


def test_the_real_home_and_real_ports_are_refused_before_any_import(tmp_path):
    """Refused in words, exit 2, and the daemon is never loaded — no daemon
    log line is printed."""
    real = pwd.getpwuid(os.getuid()).pw_dir
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    for argv, words in (
            (["--home", real, "--api-port", "29874", "--hook-port", "29873"],
             "real home"),
            (["--home", str(tmp_path / "x"), "--api-port", "19874",
              "--hook-port", "29873"], "19874"),
            (["--home", str(tmp_path / "y"), "--api-port", "29874",
              "--hook-port", "19873"], "19873")):
        done = subprocess.run([sys.executable, str(TOOL), *argv], env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 2, (argv, done.stdout, done.stderr)
        assert "REFUSED" in done.stderr and words in done.stderr
        assert "[dark-army" not in done.stderr
        assert "VERDICT" not in done.stdout
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


def test_a_hook_socket_that_exists_or_is_not_throwaway_is_refused(tmp_path):
    """The daemon clears a stale socket at its path before binding, so
    `--hook-socket` must name a new path under the throwaway home or /tmp —
    refused in words, exit 2, before any Dark Army import."""
    real = pwd.getpwuid(os.getuid()).pw_dir
    existing = tempfile.mkdtemp(prefix="da-sock-", dir="/tmp")
    taken = os.path.join(existing, "hook.sock")
    Path(taken).write_text("not a socket")
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    try:
        for sock, words in (
                (taken, "already exists"),
                (os.path.join(real, ".dark-army", "hook.sock"), "hook-socket"),
                (os.path.join(real, "da-replay-never.sock"), "real home"),
                ("/var/tmp/da-replay-never.sock", "throwaway home or /tmp")):
            done = subprocess.run(
                [sys.executable, str(TOOL), "--home", str(tmp_path / "h"),
                 "--api-port", "29874", "--hook-port", "29873",
                 "--hook-socket", sock],
                env=env, capture_output=True, text=True, timeout=60)
            assert done.returncode == 2, (sock, done.stdout, done.stderr)
            assert "REFUSED" in done.stderr and words in done.stderr, done.stderr
            assert "[dark-army" not in done.stderr
        assert Path(taken).read_text() == "not a socket"
        assert not (tmp_path / "h").exists()
    finally:
        shutil.rmtree(existing, ignore_errors=True)


def _load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("first_run_walkthrough", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def test_check_home_refuses_home_as_the_environment_names_it(tmp_path, monkeypatch):
    tool = _load_tool()
    elsewhere = tmp_path / "someone"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(elsewhere))
    with pytest.raises(tool.Refused, match="real home"):
        tool.check_home(elsewhere)
    with pytest.raises(tool.Refused, match="real home"):
        tool.check_home(tmp_path)
    assert tool.check_home(elsewhere / "walk") == elsewhere / "walk"


def test_walk_itself_refuses_the_real_home():
    """A programmatic caller gets the CLI's refusal, before anything is
    written or imported."""
    tool = _load_tool()
    saved = dict(os.environ)
    real = pwd.getpwuid(os.getuid()).pw_dir
    try:
        with pytest.raises(tool.Refused, match="real home"):
            tool.walk(real, api_port=_free_port(), hook_port=_free_port(),
                      notify_python=sys.executable, log=lambda line: None)
        assert os.environ == saved
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_walk_refuses_a_process_that_already_imported_dark_army():
    """In-process under pytest the modules point at the conftest's folder
    (and `hooks.CLAUDE_SETTINGS_PATH` at the real one): `walk` must refuse
    before it installs anything."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("first_run_walkthrough", TOOL)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    saved = dict(os.environ)
    home = tempfile.mkdtemp(prefix="da-", dir="/tmp")
    try:
        with pytest.raises(tool.Refused, match="fresh process"):
            tool.walk(home, api_port=_free_port(), hook_port=_free_port(),
                      notify_python=sys.executable, log=lambda line: None)
        assert not os.path.exists(os.path.join(home, ".dark-army"))
        assert not os.path.exists(os.path.join(home, ".claude"))
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(home, ignore_errors=True)
