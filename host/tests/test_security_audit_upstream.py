"""`tools/security_audit_upstream.py` on temporary git repositories: no
network, git under an isolated configuration."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "security_audit_upstream_cli", ROOT / "tools/security_audit_upstream.py")
tool = importlib.util.module_from_spec(_spec)
sys.modules["security_audit_upstream_cli"] = tool
_spec.loader.exec_module(tool)

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, name: str = "up") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


BASE = {
    "LICENSE": "MIT License\n\nCloudflare\n",
    "skills/security-audit/SKILL.md": "---\nname: security-audit\n---\nbody\n",
    "skills/security-audit/HUNTING.md": "hunt\n",
    "skills/security-audit/OLD.md": "old\n",
}


@pytest.fixture
def world(tmp_path):
    """An upstream repository, a first copy-in with its pin, and the paths."""
    repo = _repo(tmp_path)
    first = _commit(repo, BASE, "first")
    _git(repo, "remote", "add", "origin", tool.REPOSITORY)
    upstream = tmp_path / "template" / "upstream"
    for name, (_source, data) in tool.clone_files(repo).items():
        (upstream / name).parent.mkdir(parents=True, exist_ok=True)
        (upstream / name).write_bytes(data)
    pin = tmp_path / "template" / "upstream-pin.json"
    assert tool.cmd_pin_write(repo, upstream, pin) == 0
    return {"repo": repo, "first": first, "upstream": upstream, "pin": pin,
            "tmp": tmp_path}


def test_pin_write_records_the_commit_and_every_digest(world):
    pin = json.loads(world["pin"].read_text(encoding="utf-8"))
    assert pin["commit"] == world["first"]
    assert pin["subject"] == "first"
    assert sorted(pin["files"]) == ["HUNTING.md", "LICENSE", "OLD.md", "SKILL.md"]
    assert pin["files"]["LICENSE"]["source"] == "LICENSE"
    assert pin["files"]["SKILL.md"]["source"] == "skills/security-audit/SKILL.md"


def test_pin_check_passes_on_a_fresh_copy(world, capsys):
    assert tool.cmd_pin_check(world["upstream"], world["pin"]) == 0
    assert "pristine (" in capsys.readouterr().out


def test_pin_check_names_a_changed_file_and_an_unpinned_stray(world, capsys):
    (world["upstream"] / "HUNTING.md").write_text("hunt!\n", encoding="utf-8")
    (world["upstream"] / "EXTRA.md").write_text("x", encoding="utf-8")
    assert tool.cmd_pin_check(world["upstream"], world["pin"]) == 1
    out = capsys.readouterr().out
    assert "changed: HUNTING.md" in out and "unpinned: EXTRA.md" in out


def test_check_says_up_to_date_on_an_equal_sha(world, monkeypatch, capsys):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{world['first']}\tHEAD\n", "")

    monkeypatch.setattr(tool.subprocess, "run", fake)
    assert tool.cmd_check(world["pin"]) == 0
    assert calls == [["git", "ls-remote", tool.REPOSITORY, "HEAD"]]
    assert capsys.readouterr().out.startswith(f"up to date ({world['first'][:7]}")


def test_check_says_differs_on_another_sha(world, monkeypatch, capsys):
    other = "a" * 40
    monkeypatch.setattr(tool.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, f"{other}\tHEAD\n", ""))
    assert tool.cmd_check(world["pin"]) == 0
    assert capsys.readouterr().out.startswith(f"upstream HEAD {other} differs from the pin")


def test_check_exits_2_when_the_network_fails(world, monkeypatch, capsys):
    monkeypatch.setattr(tool.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 128, "", "fatal: unable to access"))
    assert tool.cmd_check(world["pin"]) == 2
    assert "unable to access" in capsys.readouterr().out


def test_fetch_refuses_a_non_empty_directory(world, tmp_path):
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x", encoding="utf-8")
    assert tool.cmd_fetch(full) == 2


def test_fetch_clones_a_file_url_and_prints_head(world, monkeypatch, capsys):
    monkeypatch.setattr(tool, "REPOSITORY", f"file://{world['repo']}")
    target = world["tmp"] / "fresh"
    assert tool.cmd_fetch(target) == 0
    assert capsys.readouterr().out.strip() == world["first"]
    assert (target / "LICENSE").is_file()


def _second(world) -> str:
    return _commit(world["repo"], {
        "skills/security-audit/HUNTING.md": "hunt more\n",
        "skills/security-audit/NEW-COMPANION.md": "new\n",
        "skills/security-audit/OLD.md": None,
    }, "second")


def test_diff_lists_added_removed_changed_and_the_commits(world, capsys):
    _second(world)
    assert tool.cmd_diff(world["repo"], world["pin"]) == 0
    out = capsys.readouterr().out
    assert "added: NEW-COMPANION.md" in out
    assert "removed: OLD.md" in out
    assert "changed: HUNTING.md" in out
    assert "second" in out and "commits since the pin" in out
    assert "workflow files changed: HUNTING.md" in out


def test_diff_says_the_pin_is_unreachable_when_it_is(world, capsys):
    pin = json.loads(world["pin"].read_text(encoding="utf-8"))
    pin["commit"] = "b" * 40
    world["pin"].write_text(json.dumps(pin), encoding="utf-8")
    _second(world)
    tool.cmd_diff(world["repo"], world["pin"])
    assert "the pinned commit is not within the last 100 commits" in capsys.readouterr().out


def test_apply_refuses_when_nothing_changed(world, capsys):
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("payload,needle", [
    ("see {{PROJECT}} here\n", "PLACEHOLDER"),
    ("path /Users/someone/x\n", "/Users/"),
])
def test_apply_refuses_placeholders_and_home_paths(world, capsys, payload, needle):
    _commit(world["repo"], {"skills/security-audit/HUNTING.md": payload}, "bad")
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert needle in capsys.readouterr().out
    assert (world["upstream"] / "HUNTING.md").read_text(encoding="utf-8") == "hunt\n"


def test_apply_refuses_without_a_licence(world, capsys):
    _commit(world["repo"], {"LICENSE": None,
                            "skills/security-audit/HUNTING.md": "changed\n"}, "nolicence")
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert "LICENSE" in capsys.readouterr().out


def test_apply_replaces_drops_repins_and_calls_the_sync_tool(world, monkeypatch):
    head = _second(world)
    calls: list = []
    sync_tool = world["tmp"] / "sync.py"
    # The module object is shared, so git calls (argv starting "git") are let
    # through and only the sync tool's run is recorded.
    real = subprocess.run

    def fake(argv, **kwargs):
        if argv and argv[0] == "git":
            return real(argv, **kwargs)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(tool.subprocess, "run", fake)
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"], sync_tool) == 0
    assert (world["upstream"] / "HUNTING.md").read_text(encoding="utf-8") == "hunt more\n"
    assert (world["upstream"] / "NEW-COMPANION.md").is_file()
    assert not (world["upstream"] / "OLD.md").exists()
    pin = json.loads(world["pin"].read_text(encoding="utf-8"))
    assert pin["commit"] == head
    assert "OLD.md" not in pin["files"] and "NEW-COMPANION.md" in pin["files"]
    assert tool.pin_problems(world["upstream"], world["pin"]) == []
    assert calls == [[sys.executable, str(sync_tool)]]


def test_diff_and_pin_see_a_nested_file(world, capsys):
    _commit(world["repo"], {"skills/security-audit/sub/DEEP.md": "deep\n"}, "nested")
    assert tool.cmd_diff(world["repo"], world["pin"]) == 0
    assert "added: sub/DEEP.md" in capsys.readouterr().out
    files = tool.clone_files(world["repo"])
    assert files["sub/DEEP.md"][0] == "skills/security-audit/sub/DEEP.md"


def test_a_symlink_in_the_tree_is_refused_by_name(world, capsys):
    link = world["repo"] / "skills/security-audit/LINK.md"
    link.symlink_to("HUNTING.md")
    with pytest.raises(tool.UnsafeTree, match="LINK.md is a symlink"):
        tool.clone_files(world["repo"])
    assert tool.cmd_diff(world["repo"], world["pin"]) == 1
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert "LINK.md" in capsys.readouterr().out


def test_apply_refuses_a_tree_without_the_skill_file(world, capsys):
    _commit(world["repo"], {"skills/security-audit/SKILL.md": None,
                            "skills/security-audit/HUNTING.md": "changed\n"}, "noskill")
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert "SKILL.md" in capsys.readouterr().out
    assert (world["upstream"] / "HUNTING.md").read_text(encoding="utf-8") == "hunt\n"


def test_apply_refuses_a_directory_that_is_not_cloudflares_clone(world, capsys):
    _git(world["repo"], "remote", "set-url", "origin", "https://example.com/else.git")
    _commit(world["repo"], {"skills/security-audit/HUNTING.md": "changed\n"}, "other")
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert "not a clone of" in capsys.readouterr().out
    _git(world["repo"], "remote", "remove", "origin")
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1


@pytest.mark.parametrize("which", ["skills", "skills/security-audit"])
def test_a_symlinked_skill_folder_is_refused_by_name(world, which, capsys):
    outside = world["tmp"] / "outside"
    (outside / "security-audit").mkdir(parents=True)
    (outside / "security-audit" / "SKILL.md").write_text("x", encoding="utf-8")
    (outside / "HUNTING.md").write_text("x", encoding="utf-8")
    victim = world["repo"] / which
    import shutil
    shutil.rmtree(victim)
    victim.symlink_to(outside if which == "skills" else outside / "security-audit")
    with pytest.raises(tool.UnsafeTree, match=f"{which} is a symlink"):
        tool.clone_files(world["repo"])
    assert tool.cmd_diff(world["repo"], world["pin"]) == 1
    assert tool.cmd_apply(world["repo"], world["upstream"], world["pin"],
                          world["tmp"] / "sync.py") == 1
    assert which in capsys.readouterr().out
    assert (world["upstream"] / "HUNTING.md").read_text(encoding="utf-8") == "hunt\n"
