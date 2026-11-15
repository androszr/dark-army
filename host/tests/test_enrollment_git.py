"""Enrolment starts git where there is none and makes the first commit."""
from __future__ import annotations

import subprocess

import pytest

from dark_army_daemon import enrollment, paths


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch, enforce_enrolment):
    monkeypatch.setattr(paths, "ENROLLMENT_PATH", tmp_path / "enrollment.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    enrollment.invalidate()
    yield
    enrollment.invalidate()


@pytest.fixture
def identity(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Test\n\temail = t@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
              "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def no_identity(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig-empty"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "")
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
              "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(k, raising=False)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=False)


def test_plain_folder_gets_a_repo_on_main_with_one_commit(tmp_path, identity):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    ok, detail = enrollment.enroll(str(root))
    assert ok
    assert "git started" in detail and "first commit made" in detail
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    files = git(root, "ls-files").stdout.split()
    assert "src/a.py" in files and ".gitignore" in files
    assert not any(f.startswith(".dark-army/key") for f in files)
    assert enrollment.GITIGNORE_LINE in (root / ".gitignore").read_text()
    assert git(root, "status", "--porcelain").stdout.strip() == ""


def test_existing_repo_with_commits_is_left_alone(tmp_path, identity):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "f.txt").write_text("hi\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "mine")
    before = git(root, "rev-parse", "HEAD").stdout
    ok, detail = enrollment.enroll(str(root))
    assert ok and "first commit" not in detail
    assert git(root, "rev-parse", "HEAD").stdout == before
    assert git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_empty_head_repo_gets_the_first_commit(tmp_path, identity):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-b", "trunk")
    (root / "f.txt").write_text("hi\n")
    ok, detail = enrollment.enroll(str(root))
    assert ok and "first commit made" in detail and "git started" not in detail
    assert git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert "f.txt" in git(root, "ls-files").stdout


def test_a_folder_inside_another_repo_is_never_nested(tmp_path, identity):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    git(outer, "init", "-b", "main")
    (inner / "f.txt").write_text("hi\n")
    ok, detail = enrollment.enroll(str(inner))
    assert ok and "git started" not in detail and "first commit" not in detail
    assert not (inner / ".git").exists()
    assert git(outer, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def test_no_identity_enrols_with_a_note_and_no_commit(tmp_path, no_identity):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "f.txt").write_text("hi\n")
    ok, detail = enrollment.enroll(str(root))
    assert ok
    assert "git started" in detail
    assert "no first commit made" in detail
    assert git(root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    assert enrollment.root_enrolled(str(root))


def test_a_git_that_will_not_run_never_fails_the_enrolment(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()

    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(enrollment.subprocess, "run", boom)
    ok, _ = enrollment.enroll(str(root))
    assert ok


def test_secret_files_stay_out_of_the_first_commit_and_the_note_says_so(
        tmp_path, identity):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    (root / ".env").write_text("TOKEN=1\n")
    (root / "cert.pem").write_text("pem\n")
    (root / "sub").mkdir()
    (root / "sub" / "id_rsa").write_text("k\n")
    (root / "sub" / "deploy.key").write_text("k\n")
    ok, detail = enrollment.enroll(str(root))
    assert ok and "first commit made" in detail
    files = git(root, "ls-files").stdout.split()
    assert "a.txt" in files
    assert not {".env", "cert.pem", "sub/id_rsa", "sub/deploy.key"} & set(files)
    assert "left out as secrets" in detail and ".env" in detail


def test_the_commit_is_refused_when_the_key_is_not_ignored(
        tmp_path, identity, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("a\n")
    monkeypatch.setattr(enrollment, "_append_gitignore", lambda r: "")
    real = enrollment._write_key_dir

    def bare(here):
        real(here)
        inner = here / enrollment.KEY_DIR_NAME / ".gitignore"
        if inner.exists():
            inner.unlink()
    monkeypatch.setattr(enrollment, "_write_key_dir", bare)
    ok, detail = enrollment.enroll(str(root))
    assert ok and "is not ignored by git" in detail
    assert git(root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0


def test_the_first_commit_neither_signs_nor_runs_hooks(tmp_path, identity):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-b", "main")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    git(root, "config", "commit.gpgsign", "true")
    git(root, "config", "gpg.program", "/nonexistent-gpg")
    (root / "f.txt").write_text("hi\n")
    ok, detail = enrollment.enroll(str(root))
    assert ok and "first commit made" in detail
    assert git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_a_timeout_is_named_and_removes_only_a_lock_it_did_not_find(
        tmp_path, identity, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-b", "main")
    lock = root / ".git" / "index.lock"
    real = subprocess.run

    def slow(argv, *a, **k):
        if "add" in argv:
            lock.write_text("")
            raise subprocess.TimeoutExpired(argv, 60)
        return real(argv, *a, **k)
    monkeypatch.setattr(enrollment.subprocess, "run", slow)
    note = enrollment._first_commit(root)
    assert "timed out after 60 seconds" in note
    assert "git did not run" not in note
    assert not lock.exists()
    # A lock that was already there is somebody else's: left alone.
    lock.write_text("held")
    note = enrollment._first_commit(root)
    assert "timed out" in note and lock.exists()


def test_git_that_did_not_run_is_told_from_a_timeout(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(enrollment.subprocess, "run", boom)
    assert enrollment._run_git(tmp_path, ["status"]) is None
    assert enrollment._why(None, "x") == "git did not run"
    assert "timed out" in enrollment._why(enrollment._TIMEOUT, "x")
