"""Every child Dark Army starts gets a login shell's PATH.

Dark Army and its pty broker are started by launchd, so they inherit the
bare `/usr/bin:/bin:/usr/sbin:/sbin`; macOS adds `/etc/paths.d` (Homebrew's
`/opt/homebrew/bin`) only in a login shell. On 22 Sep 2026 Mission Control,
on Dark Army's own terminal, could not find `node` on a Mac that has it, and
72 host tests failed as "no node". `clean_env` now appends the login entries.
"""

import os

from dark_army_daemon import ptyhost, subprocess_env


def _etc(tmp_path):
    paths = tmp_path / "paths"
    paths.write_text("/usr/local/bin\n/usr/bin\n/bin\n\n# a comment\n")
    folder = tmp_path / "paths.d"
    folder.mkdir()
    (folder / "20-z").write_text("/opt/z/bin\n")
    (folder / "homebrew").write_text("/opt/homebrew/bin\n")
    (folder / "10-a").write_text("/opt/a/bin\n/usr/bin\n")
    return str(paths), str(folder)


def test_entries_follow_path_helper_order(tmp_path):
    paths, folder = _etc(tmp_path)
    assert subprocess_env.login_path_entries(paths, folder) == [
        "/usr/local/bin", "/usr/bin", "/bin", "/opt/a/bin", "/opt/z/bin",
        "/opt/homebrew/bin"]


def test_missing_files_contribute_nothing(tmp_path):
    assert subprocess_env.login_path_entries(
        str(tmp_path / "nope"), str(tmp_path / "nodir")) == []


def test_login_entries_are_appended_and_what_is_there_keeps_its_place():
    got = subprocess_env.with_login_path(
        "/Users/me/.local/bin:/usr/bin:/bin", ["/usr/bin", "/opt/homebrew/bin"])
    assert got == "/Users/me/.local/bin:/usr/bin:/bin:/opt/homebrew/bin"
    assert subprocess_env.with_login_path("", ["/opt/homebrew/bin"]) == "/opt/homebrew/bin"


def test_clean_env_gives_a_launchd_path_the_homebrew_folder(tmp_path, monkeypatch):
    paths, folder = _etc(tmp_path)
    monkeypatch.setattr(subprocess_env, "PATHS_FILE", paths)
    monkeypatch.setattr(subprocess_env, "PATHS_DIR", folder)
    env = subprocess_env.clean_env({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/h"})
    parts = env["PATH"].split(os.pathsep)
    assert parts[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert "/opt/homebrew/bin" in parts
    assert env["HOME"] == "/h"
    no_path = subprocess_env.clean_env({"HOME": "/h"})
    assert "/opt/homebrew/bin" in no_path["PATH"].split(os.pathsep)


def test_the_real_machine_file_names_are_the_macos_ones():
    # `conftest` points these at nothing for the rest of the suite, so the
    # shipped values are checked in the source.
    src = open(subprocess_env.__file__, encoding="utf-8").read()
    assert 'PATHS_FILE = "/etc/paths"' in src
    assert 'PATHS_DIR = "/etc/paths.d"' in src


def test_the_pty_host_builds_its_child_env_through_clean_env():
    """The terminal every agent runs on: its child environment is
    `clean_env`'s, so the login PATH reaches Mission Control and card runs."""
    src = open(ptyhost.__file__, encoding="utf-8").read()
    assert "child_env = subprocess_env.clean_env(env)" in src


# ── the process itself, not only its children ────────────────────────────────

def test_adopt_login_path_appends_the_login_entries_to_the_process(tmp_path, monkeypatch):
    from dark_army_daemon import subprocess_env
    paths = tmp_path / "paths"
    paths.write_text("/usr/bin\n/bin\n")
    folder = tmp_path / "paths.d"
    folder.mkdir()
    (folder / "homebrew").write_text("/opt/homebrew/bin\n")
    monkeypatch.setattr(subprocess_env, "PATHS_FILE", str(paths))
    monkeypatch.setattr(subprocess_env, "PATHS_DIR", str(folder))
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    assert subprocess_env.adopt_login_path(env) == \
        "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    subprocess_env.adopt_login_path(env)          # idempotent
    assert env["PATH"].count("/opt/homebrew/bin") == 1


def test_the_app_and_the_broker_adopt_it_first_thing():
    """A broker outlives app restarts, so the fix must live in each
    process's own start — not only in the env builder a stale broker may
    not have loaded."""
    import inspect
    from dark_army_daemon import pty_broker
    from dark_army_menubar import app
    for main in (pty_broker.main, app.main):
        body = inspect.getsource(main)
        assert "subprocess_env.adopt_login_path()" in body
        first_lines = "\n".join(body.splitlines()[:8])
        assert "adopt_login_path()" in first_lines
