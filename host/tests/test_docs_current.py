"""The four front-door documents must keep describing the product that ships.

Two checks only, and both are the kind a rename breaks silently:

1. every repository path these documents cite can still be opened, and
2. a short list of *retired* wording stays gone.

Precedent is ``test_review_skill.py``, which already pins documentation from
this suite. Nothing here imports the daemon, touches the state directory or
asserts anything about the app's behaviour — it reads Markdown off disk.
"""

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# `plans/` is git-ignored: a fresh clone (CI) has none, so a plan citation is
# checked only on a machine that keeps its plans. CI never counts as one, even
# when a branch force-adds a single plan and so creates the folder (run
# 35828881336, 23 Sep 2026: one committed plan made every other citation fail).
PLANS = REPO_ROOT / "plans"
PLANS_KEPT = PLANS.is_dir() and not os.environ.get("CI")

DOCS = (
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "GEMINI.md",
    # The contract documents lifted out of CLAUDE.md on 6 Sep 2026.
    "docs/transport-contract.md",
    "docs/codex-contract.md",
    "docs/phone-contract.md",
    "docs/menubar-strip-contract.md",
    "docs/panel-window-contract.md",
    "docs/session-state-contract.md",
    # Lifted out for the same reason on 7 Sep 2026, when the knowledge notes
    # had to fit inside the file's own 140k ceiling.
    "docs/channel-tools.md",
    "docs/knowledge-notes.md",
    # The dated audits cite files by path too; the first one to be pinned.
    "docs/2026-09-20-relay-latency-audit.md",
    # The front page's detail and its security section moved here on
    # 22 Sep 2026, when the README was rewritten for people running agents.
    "docs/reference.md", "SECURITY.md",
    "docs/cli-permission-modes.md",
    "docs/2026-09-23-codex-permission-hold-verification.md",
    # Card dependencies' long-form contract, 24 Sep 2026.
    "docs/card-dependencies.md",
    # The harness token policy, 25 Sep 2026: every loop it maps cites a path.
    "docs/harness-token-policy.md",
)

# Scan the *raw* text rather than backtick pairs. A naive `` `([^`]+)` ``
# pairing is thrown off by fenced code blocks — the fence's own backticks
# re-pair the ones inside it — and silently misses most of the citations
# (measured: 2 of 11). The alternation of top-level directory names is what
# keeps `~/.bob-companion/...` and `~/.claude/settings.json` out, and the
# character class deliberately excludes `*` so a glob such as
# `assets/proposals/.../*.gif` is not read as a path.
PATH_RE = re.compile(
    r"(?<![\w/~.-])"
    r"((?:host|panel|tools|ios|relay|vscode-extension|assets|plans)/"
    r"[\w./-]*\.(?:py|swift|sh|json|md|toml))"
)

# (filename, phrase) pairs, each retired rather than merely absent today. A
# blocklist entry for wording that could legitimately come back is a test that
# gets deleted instead of obeyed, so every entry says what replaced it.
RETIRED = (
    # The third listener binds 0.0.0.0:19875 whenever phone access is on.
    ("README.md", "nothing listens on an external interface"),
    # There has never been a docked-sidebar preference; the panel is a window.
    ("README.md", "docked sidebar"),
    # The Terminal-titles toggle was removed: the pen is taken on every launch
    # and never given back. If that toggle is ever reinstated, delete this row.
    ("README.md", "Terminal titles"),
    # The edge ribbon and its menu row are gone.
    ("README.md", "Edge ribbon"),
    # The channel exposes four board tools, not three.
    ("README.md", "three narrow ways"),
    # Settings is a window with a search box (⌘,), not a popup menu.
    ("README.md", "⋯ menu"),
    # GitHub is the only remote; GitLab was dropped 15 Aug 2026.
    ("CONTRIBUTING.md", "merge requests"),
    # There is an iPhone app and a relay mailbox.
    ("AGENTS.md", "No device, no Bluetooth, no network dependency"),
    # Board.swift was split into BoardView/BoardState/BoardLanes/BoardDrop.
    ("AGENTS.md", "Board.swift"),
    # The render engine's retelling: deleted 15 Aug 2026, and CLAUDE.md's own
    # rule is to drop the origin story the fact replaced.
    ("GEMINI.md", "LVGL"),
    ("GEMINI.md", "RGB565"),
    ("GEMINI.md", "ESP32"),
    # The phone is the third surface.
    ("GEMINI.md", "Two surfaces read it"),
    # Since 20 Sep 2026 the buzz's third line (`need`) rides as the body.
    ("docs/transport-contract.md", "`aps.alert.body` stays unused"),
    # The root's ceiling is 30,000 UTF-8 bytes since 20 Sep 2026
    # (test_claude_md_size.py); "140k" was the 6 Sep 2026 figure.
    ("docs/knowledge-notes.md", "held under 140k"),
    ("docs/channel-tools.md", "held under 140k"),
    # There is no folder concept on the board; cards sit in four rows.
    ("README.md", "grouped into folders"),
    # The diagnostics live under one Advanced heading in the settings window.
    ("README.md", "→ **Troubleshooting**"),
    # A Claude session gets eight channel tools, Codex four, Grok none.
    ("README.md", "four narrow ways"),
    ("docs/reference.md", "four narrow ways"),
    # A stopped native Codex session can take an opt-in reply.
    ("README.md", "never replied to"),
    # Settings is a sidebar of eight sections since 26 Sep 2026: Pipeline is
    # under Board, Agent models is Models, and Restart / Quit / the kill
    # switch sit at the foot of the sidebar.
    ("README.md", "Settings → **Pipeline**"),
    ("README.md", "Settings → **This app**"),
    ("README.md", "Settings → **Agent models**"),
    ("docs/reference.md", "one scrollable page of headed groups"),
    ("docs/kill-switch.md", "settings page's first row"),
)

# A hard-coded test count decays by the week; the commands stay, the number goes.
TEST_COUNT_RE = re.compile(r"~?[\d,]+\s+tests")


def _read(name: str) -> str:
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not present in this checkout")
    return path.read_text(encoding="utf-8")


def test_every_cited_repo_path_exists():
    """A rename must not leave these documents pointing at a missing file."""
    missing = []
    for name in DOCS:
        for cited in PATH_RE.findall(_read(name)):
            if cited.startswith("plans/") and not PLANS_KEPT:
                continue
            if not (REPO_ROOT / cited).exists():
                missing.append(f"{name} cites {cited}, which does not exist")
    assert not missing, "\n".join(missing)


def _flat(text: str) -> str:
    """Collapse every run of whitespace, so a phrase split across a wrapped
    line is still found. Without this, "nothing listens on an\nexternal
    interface" hides from its own blocklist entry and the row is decoration."""
    return " ".join(text.split())


def test_retired_phrases_are_absent():
    """Wording that described a removed feature must not come back."""
    found = []
    for name, phrase in RETIRED:
        if _flat(phrase) in _flat(_read(name)):
            found.append(f"{name} still says {phrase!r}")
    readme = _read("README.md")
    for stale in TEST_COUNT_RE.findall(_flat(readme)):
        found.append(f"README.md states a test count ({stale.strip()!r}); "
                     "counts decay — name the command, not the number")
    assert not found, "\n".join(found)
