"""The ancestor walk that names a session's own process, over a made-up
process table: `ps` never runs, and `os.getppid` is ours to choose."""

import subprocess
from types import SimpleNamespace

import pytest

from dark_army_daemon import pid_resolver
from dark_army_daemon.pid_resolver import (
    find_claude_pid,
    find_session_pid,
    is_grok_leader,
    looks_like_grok,
    looks_like_session,
)

LEADER = "/Users/me/.grok/bin/grok agent leader --no-exit-on-disconnect --relay-on-demand"


class ProcessTable:
    """`ps -o <field>= -p <pid>` answered from a dict of pid -> fields."""

    def __init__(self, monkeypatch, parent, rows):
        self.rows = rows
        self.asked = []
        monkeypatch.setattr(pid_resolver.os, "getppid", lambda: parent)
        monkeypatch.setattr(pid_resolver.subprocess, "run", self._ps)
        monkeypatch.delenv("GROK_SESSION_ID", raising=False)

    def _ps(self, argv, **_kw):
        field, pid = argv[2].rstrip("="), int(argv[4])
        self.asked.append((field, pid))
        value = str(self.rows.get(pid, {}).get(field, ""))
        # Bytes, as the real call returns them: `_ps` decodes by hand.
        return SimpleNamespace(stdout=(value + "\n").encode(), returncode=0)


def _proc(comm, command="", ppid=1):
    return {"comm": comm, "command": command or comm, "ppid": ppid}


# -- finding Claude ----------------------------------------------------------

@pytest.mark.parametrize("parent, rows, expected", [
    # The hook's own parent is the native binary.
    (10, {10: _proc("claude")}, 10),
    # A shell sits between the hook and the session.
    (20, {20: _proc("zsh", "zsh -c ~/.dark-army/dark-army-notify", 21),
          21: _proc("claude")}, 21),
    # Two wrappers deep.
    (30, {30: _proc("env", "env -u PYTHONHOME python3 x", 31),
          31: _proc("zsh", "zsh", 32), 32: _proc("claude")}, 32),
    # A node-wrapped install: the name is node, the program is `claude`.
    (40, {40: _proc("node", "node /opt/claude-code/bin/claude --resume r1")}, 40),
    (50, {50: _proc("launcher", "/usr/local/bin/claude --continue")}, 50),
], ids=["parent", "one-shell", "two-wrappers", "node-wrapped", "absolute-path"])
def test_the_nearest_claude_ancestor_is_the_session(monkeypatch, parent, rows, expected):
    ProcessTable(monkeypatch, parent, rows)
    assert find_claude_pid() == expected


@pytest.mark.parametrize("rows", [
    # A Bash-tool shell mentions claude in a path; it is not the session.
    {60: _proc("zsh", "zsh -c source ~/.claude/shell-snapshots/snap-1.sh; ls")},
    {60: _proc("vim", "vim /tmp/claude-notes.txt")},
    {60: _proc("zsh", "zsh")},
    {},
], ids=["snapshot-shell", "editor", "plain-shell", "ps-says-nothing"])
def test_with_no_claude_ancestor_the_answer_is_our_parent(monkeypatch, rows):
    ProcessTable(monkeypatch, 60, rows)
    assert find_claude_pid() == 60


def test_a_native_name_settles_it_without_reading_the_command_line(monkeypatch):
    table = ProcessTable(monkeypatch, 70, {70: _proc("claude")})
    find_claude_pid()
    assert ("command", 70) not in table.asked


def test_a_ps_that_times_out_falls_back_to_our_parent(monkeypatch):
    def hang(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=1.0)

    monkeypatch.setattr(pid_resolver.os, "getppid", lambda: 80)
    monkeypatch.setattr(pid_resolver.subprocess, "run", hang)
    assert find_claude_pid() == 80


def test_a_command_line_that_is_not_utf8_still_reads(monkeypatch):
    raw = "/usr/bin/vim /Users/x/caf\xe9/notes.txt\n".encode("latin-1")
    monkeypatch.setattr(pid_resolver.subprocess, "run",
                        lambda *_a, **_kw: SimpleNamespace(stdout=raw, returncode=0))
    assert pid_resolver._ps("command", 5).startswith("/usr/bin/vim")


# -- Grok and its leader -----------------------------------------------------

@pytest.mark.parametrize("command, leader", [
    (LEADER, True),
    ("grok agent leader", True),
    ("grok agent --leader stdio", False),
    ("grok", False),
    ("", False),
])
def test_only_the_agent_leader_subcommand_is_the_leader(command, leader):
    assert is_grok_leader(command) is leader


@pytest.mark.parametrize("comm, command, grok", [
    ("grok", "", True),
    ("grok", "grok", True),
    ("grok", "/opt/homebrew/bin/grok --resume g1", True),
    ("grok", LEADER, False),
    ("zsh", "", False),
])
def test_what_counts_as_a_grok_session(comm, command, grok):
    assert looks_like_grok(comm, command) is grok


@pytest.mark.parametrize("comm, provider, accepted", [
    ("grok", "grok", True),
    ("claude", "grok", False),
    ("grok", "claude", False),
    ("grok", None, True),
    ("claude", None, True),
    ("nginx", None, False),
])
def test_a_known_provider_narrows_the_identity_check(comm, provider, accepted):
    assert looks_like_session(comm, comm, provider) is accepted


def test_the_leader_is_refused_even_for_a_grok_session():
    assert looks_like_session("grok", LEADER, "grok") is False


def test_grok_is_found_when_grok_is_preferred(monkeypatch):
    ProcessTable(monkeypatch, 90, {90: _proc("zsh", "zsh", 91), 91: _proc("grok")})
    assert find_session_pid(prefer="grok") == (91, "grok")


def test_the_walk_stops_at_the_leader_rather_than_claim_another_tab(monkeypatch):
    # The shared leader is parented under some other tab's TUI.
    ProcessTable(monkeypatch, 100, {100: _proc("grok", LEADER, 101), 101: _proc("grok")})
    assert find_session_pid(prefer="grok") == (100, "grok")


def test_a_leader_with_nothing_above_it_is_never_the_answer(monkeypatch):
    ProcessTable(monkeypatch, 110, {110: _proc("python3", "python3 dark-army-notify", 111),
                                    111: _proc("grok", LEADER, 1)})
    assert find_session_pid(prefer="grok") == (110, "grok")


def test_a_claude_walk_passing_a_leader_does_not_take_the_tab_above_it(monkeypatch):
    ProcessTable(monkeypatch, 120, {
        120: _proc("python3", "python3 -m pytest", 121),
        121: _proc("grok", LEADER, 122),
        122: _proc("grok", "grok", 1),
    })
    assert find_session_pid(prefer="claude") == (120, "claude")
