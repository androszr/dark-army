"""There is no changelog, and nothing an agent reads asks for one.

The project dropped its changelog on 22 Sep 2026
(`plans/2026-09-22-scrap-changelog.md`): a release's notes are written at the
moment it is cut, from the commits since the previous tag, by the recipe in
the releasing skill. These checks keep the file from coming back, keep every
live instruction from asking for an entry again, and run the recipe itself on
a small made-up history so a broken snippet is caught before a release.

History is left alone on purpose: dated reports under ``docs/`` and
``plans/`` may say "changelog" and are not scanned. Like
``test_docs_current.py``, nothing here imports the product; it reads files off
disk and, for the recipe, runs ``git`` in a throwaway repository.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SKILL = REPO_ROOT / ".claude/skills/releasing/SKILL.md"
SKILL_TWIN = REPO_ROOT / ".agents/skills/releasing/SKILL.md"

#: Root files an agent or a contributor reads as instructions.
ROOT_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "README.md",
    "CONTRIBUTING.md",
    "host/build.sh",
    "host/setup.py",
)

#: Trees of live instructions, briefs, skills and tools.
TREES = (
    ".claude",
    ".agents",
    ".codex",
    ".grok/agents",
    "tools",
    "host/dark_army_menubar/agent_pack",
    "docs",
)

SUFFIXES = {".md", ".toml", ".json", ".py", ".sh", ".yaml", ".yml", ".txt"}

#: A dated report under docs/ is history: it says what was true on its day.
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}-")

_WORD = re.compile(r"changelog", re.I)


#: Variables that point git at some other repository or index. Dropped from
#: every subprocess here: the listing must read this checkout, and the
#: made-up history must never land in it.
_GIT_LOCATORS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATORS}


def _live_files() -> list[Path]:
    """Tracked files plus new ones git does not ignore, under the root files
    and trees above. Git-ignored machine-local files (a resume note, a local
    review file, a worktree checkout under `.claude/worktrees/`) are not the
    project's instructions and are not scanned."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH; the live file list comes from git")
    listed = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard", "--",
         *ROOT_FILES, *TREES],
        cwd=REPO_ROOT, env=_clean_env(), capture_output=True, text=True,
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout; the live file list comes from git")
    files = []
    for rel in sorted(set(filter(None, listed.stdout.split("\0")))):
        path = REPO_ROOT / rel
        parts = Path(rel).parts
        if rel not in ROOT_FILES:
            if path.suffix not in SUFFIXES or "__pycache__" in parts:
                continue
            if parts[0] == "docs" and _DATED.match(path.name):
                continue
        if path.is_file():
            files.append(path)
    assert any(f.name == "CLAUDE.md" for f in files), "git listed no live files"
    return files


def test_the_changelog_file_is_gone() -> None:
    assert not (REPO_ROOT / "CHANGELOG.md").exists(), (
        "CHANGELOG.md is back; release notes are written from the commits "
        "(the releasing skill's `# release-notes` step)"
    )


def test_no_live_instruction_mentions_a_changelog() -> None:
    hits = []
    for path in _live_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if _WORD.search(line):
                rel = path.relative_to(REPO_ROOT)
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "a live instruction mentions a changelog:\n" + "\n".join(hits)


def _notes_block(text: str) -> str:
    match = re.search(r"^```bash\n(# release-notes\n.*?)^```", text, re.S | re.M)
    assert match, "the releasing skill has no fenced block starting `# release-notes`"
    return match.group(1)


def test_both_releasing_skills_are_one_text() -> None:
    assert SKILL.read_bytes() == SKILL_TWIN.read_bytes(), (
        "cp .claude/skills/releasing/SKILL.md .agents/skills/releasing/SKILL.md"
    )
    text = SKILL.read_text(encoding="utf-8")
    assert "--notes-file host/dist/release-notes.md" in text
    assert "tag=vX.Y.Z" in _notes_block(text)


def _git(repo: Path, env: dict, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                   capture_output=True, text=True)


def _run_recipe(repo: Path, env: dict, tag: str) -> str:
    block = _notes_block(SKILL.read_text(encoding="utf-8"))
    assert block.count("tag=vX.Y.Z") == 1
    script = block.replace("tag=vX.Y.Z", f"tag={tag}")
    subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env,
                   check=True, capture_output=True, text=True)
    notes = repo / "host/dist/release-notes.md"
    assert notes.is_file()
    return notes.read_text(encoding="utf-8")


def _changes(notes: str) -> list[str]:
    after = notes.split("## Every change", 1)[1]
    return [line for line in after.splitlines() if line.startswith("- ")]


def test_release_notes_recipe_on_a_made_up_history(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    env = {
        **_clean_env(),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    for n, subject in enumerate(("First thing.", "Second thing.", "Third thing.")):
        (repo / "file.txt").write_text(f"{n}\n")
        _git(repo, env, "add", "file.txt")
        _git(repo, env, "commit", "-q", "-m", subject)
        if n == 0:
            _git(repo, env, "tag", "v1.0.0")
    _git(repo, env, "tag", "v1.1.0")

    later = _run_recipe(repo, env, "v1.1.0")
    assert "## Highlights" in later
    assert _changes(later) == ["- Third thing.", "- Second thing."]
    compare = [line for line in later.splitlines() if line.startswith("Compare:")]
    assert len(compare) == 1 and compare[0].endswith("v1.0.0...v1.1.0")

    first = _run_recipe(repo, env, "v1.0.0")
    assert "## Highlights" in first
    assert _changes(first) == ["- First thing."]
    assert "Compare:" not in first
