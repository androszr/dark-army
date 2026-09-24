"""The to-do file and its archive are retired, and nothing an agent reads asks
for them.

On 22 Sep 2026 (``plans/2026-09-22-retire-todo-md.md``) the project stopped
keeping ``TODO.md`` and ``TODO-archive.md``: follow-ups are Prep cards on the
board, what was built is written up in ``plans/``, and measurements go to the
project's knowledge notes or the subject documents. Several sessions edit
this checkout at once, and one that loaded the old ``CLAUDE.md`` will try to
"update ``TODO.md``" when it finishes — these checks are the tripwire that
catches the file coming back, and the rule coming back with it.

The bounded-read discipline that used to sit beside the TODO rule is kept on
purpose, so the last check pins that it survived. History — ``plans/``, the
dated reports under ``docs/``, the ship-efficiency preservation map and its
fixtures — may name the old file and is not scanned. Stdlib and pytest only;
nothing here imports the product.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

RETIRED = ("TODO.md", "TODO-archive.md")

#: The live instruction files and trees, the same set as the plan's grep.
ROOT_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "CONTRIBUTING.md",
    "README.md",
    "panel/Package.swift",
)
TREES = (
    ".claude",
    ".agents",
    ".codex",
    "docs",
    "host/dark_army_menubar",
    "host/dark_army_daemon",
    "host/tests",
    "tools",
)

#: History and this file itself: allowed to name the old files.
EXCLUDED_DIRS = {"fixtures"}
EXCLUDED_NAMES = ("2026-*.md", "ship-efficiency.md", "test_todo_retired.py")

LEAD_DIRS = (
    ".claude/leads",
    "host/dark_army_menubar/agent_pack/template/.claude/leads",
)

#: Every copy of the bounded-read rule the retirement had to keep.
BOUNDED_READ_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    ".claude/skills/ship/references/common.md",
    "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/references/common.md",
)

_MENTION = re.compile(rb"TODO(-archive)?\.md")

#: Variables that point git at some other repository or index; dropped so
#: the listing reads this checkout.
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


def _excluded(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT)
    if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDED_NAMES)


def _live_files() -> list[Path]:
    """Tracked files plus new ones git does not ignore, under the root files
    and trees above. Git-ignored machine-local state (a resume note, a local
    review file, a worktree checkout under `.claude/worktrees/`) is not the
    project's instructions and is not scanned."""
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
        if path.is_file() and not _excluded(path):
            files.append(path)
    assert any(f.name == "CLAUDE.md" for f in files), "git listed no live files"
    return files


@pytest.mark.parametrize("name", RETIRED)
def test_the_retired_file_is_gone(name: str) -> None:
    assert not (REPO_ROOT / name).exists(), (
        f"{name} is back: a session loaded before 22 Sep 2026 recreated it — "
        "file anything live as a Prep card and delete it"
    )


def test_no_live_instruction_names_a_todo_file() -> None:
    offenders = []
    for path in _live_files():
        data = path.read_bytes()
        if b"\0" in data:  # binary, as `grep -I` treats it
            continue
        if _MENTION.search(data):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "these live files still name the retired to-do file: "
        f"{offenders}. Say the thing itself, or name the plan that holds it."
    )


def test_no_lead_brief_mentions_a_todo() -> None:
    briefs = [p for d in LEAD_DIRS for p in sorted((REPO_ROOT / d).glob("*.md"))]
    assert briefs, "no lead briefs found"
    for brief in briefs:
        assert "TODO" not in brief.read_text(encoding="utf-8"), (
            f"{brief.relative_to(REPO_ROOT)} still asks for a to-do read"
        )


def test_the_root_has_no_todo_section_and_keeps_the_ci_fact() -> None:
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## TODO Tracking" not in text
    assert "ruff check ." in text, "the CI sentence moved to '## Build and test'"


def test_the_context_map_selects_no_todo_file() -> None:
    data = json.loads((REPO_ROOT / "docs/agent-context.json").read_text(encoding="utf-8"))
    assert "todo" not in data, "docs/agent-context.json still carries the 'todo' block"
    assert "TODO" not in json.dumps(data), "docs/agent-context.json still names a to-do file"


@pytest.mark.parametrize("name", BOUNDED_READ_FILES)
def test_the_bounded_read_discipline_survived(name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert "incomplete read" in text, f"{name} lost the bounded-read rule"
