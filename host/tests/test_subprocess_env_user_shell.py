"""An agent on Dark Army's terminal starts through the person's own shell,
so what their profile exports — a Keychain key among them — reaches it.

18 Oct 2026: ``MESHY_API_KEY``, exported in ``~/.zshrc`` from the Keychain,
was missing in every card's session, because the broker and its terminals
start under launchd and no profile ever ran."""
import asyncio
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dark_army_daemon import subprocess_env  # noqa: E402
from dark_army_daemon.ptyhost import PtyHost  # noqa: E402


@pytest.fixture
def shell_on(monkeypatch):
    monkeypatch.setattr(subprocess_env, "USER_SHELL_ENV", True)


def test_the_argv_runs_through_the_shell_which_execs_it(shell_on):
    assert subprocess_env.shell_argv(["claude", "--x"], shell="/bin/zsh") == [
        "/bin/zsh", "-l", "-i", "-c", 'exec "$@"', "zsh", "claude", "--x"]


def test_unchanged_when_off_empty_or_no_known_shell(monkeypatch, shell_on):
    assert subprocess_env.shell_argv([], shell="/bin/zsh") == []
    assert subprocess_env.shell_argv(["a"], shell="") == ["a"]
    monkeypatch.setattr(subprocess_env, "USER_SHELL_ENV", False)
    assert subprocess_env.shell_argv(["a"], shell="/bin/zsh") == ["a"]


def test_the_suite_never_runs_the_machine_profile():
    """conftest turns the wrap off for every test."""
    assert subprocess_env.shell_argv(["a"]) == ["a"]


@pytest.mark.parametrize("shell,want", [
    ("/bin/zsh", "/bin/zsh"),
    ("/bin/bash", "/bin/bash"),
    ("/opt/homebrew/bin/fish", ""),
    ("zsh", ""),
    ("", ""),
])
def test_only_a_known_absolute_shell_is_used(monkeypatch, shell, want):
    class Entry:
        pw_shell = shell
    monkeypatch.setattr(subprocess_env.pwd, "getpwuid", lambda uid: Entry())
    assert subprocess_env.user_shell() == want


def test_an_unreadable_account_entry_means_no_shell(monkeypatch):
    def boom(uid):
        raise KeyError(uid)
    monkeypatch.setattr(subprocess_env.pwd, "getpwuid", boom)
    assert subprocess_env.user_shell() == ""


@pytest.mark.skipif(not os.access("/bin/zsh", os.X_OK), reason="no zsh")
def test_the_profile_export_reaches_the_agent_and_the_pid_is_kept(
        tmp_path, shell_on):
    """A real zsh, a throwaway profile: the export the profile computes is
    in the program's environment, and the pid is the program's own."""
    (tmp_path / ".zshrc").write_text(
        'export DARK_ARMY_TEST_KEY="$(echo from-profile)"\n')
    argv = subprocess_env.shell_argv(
        ["/bin/sh", "-c", 'echo "$$ $DARK_ARMY_TEST_KEY"'], shell="/bin/zsh")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "ZDOTDIR": str(tmp_path)}
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    out, _ = proc.communicate(timeout=20)
    pid, value = out.strip().splitlines()[-1].split(" ", 1)
    assert int(pid) == proc.pid
    assert value == "from-profile"


def test_the_broker_is_sent_the_shell_wrapped_argv(monkeypatch, shell_on):
    monkeypatch.setattr(subprocess_env, "user_shell", lambda: "/bin/zsh")
    host = PtyHost(persist=True, sock_path="/tmp/unused-dark-army-test.sock")
    host._broker_login_path = True
    sent = {}

    async def attach():
        return True

    async def rpc(op, **kw):
        sent.update(kw, op=op)
        return {"ok": False, "detail": "stub"}

    host.attach = attach
    host._rpc = rpc
    asyncio.run(host._broker_start("/tmp", ["claude", "go"], "agent"))
    assert sent["op"] == "start"
    assert sent["argv"] == ["/bin/zsh", "-l", "-i", "-c", 'exec "$@"', "zsh",
                            "claude", "go"]
