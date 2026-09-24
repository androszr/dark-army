"""The Prepare agent and its twins stay in step with the shipped brief."""
from __future__ import annotations

import subprocess
from pathlib import Path

from dark_army_daemon import card_prepare, card_preparer_brief

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / ".claude" / "agents" / "bc-card-preparer.md"
CODEX = REPO / ".codex" / "agents" / "bc-card-preparer.toml"
GROK = REPO / ".grok" / "agents" / "bc-card-preparer.md"
TEMPLATE = (
    REPO / "host" / "dark_army_menubar" / "agent_pack" / "template"
    / ".claude" / "agents" / "{{P}}-card-preparer.md"
)


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n")


def _folded_description(path: Path) -> str:
    parsed = card_prepare.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert parsed is not None
    return parsed[1]


def test_repo_agent_body_matches_the_shipped_brief():
    assert _body(AGENT) == card_preparer_brief.BRIEF


def test_pack_template_body_matches_the_shipped_brief():
    assert _body(TEMPLATE) == card_preparer_brief.BRIEF


def test_repo_description_matches_the_shipped_constant():
    assert _folded_description(AGENT) == card_preparer_brief.DESCRIPTION


def test_codex_twin_exists_and_names_the_markdown():
    text = CODEX.read_text(encoding="utf-8")
    assert 'name = "bc-card-preparer"' in text
    assert ".claude/agents/bc-card-preparer.md" in text


def test_grok_twin_exists_and_names_the_markdown():
    text = GROK.read_text(encoding="utf-8")
    assert "name: bc-card-preparer" in text
    assert ".claude/agents/bc-card-preparer.md" in text


def test_the_grok_twin_is_not_git_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".grok/agents/bc-card-preparer.md"],
        cwd=REPO)
    assert result.returncode == 1


def test_the_brief_says_dark_army_and_never_the_old_name():
    """The helper is told who it drafts for by the product's name; the old
    one is gone from model-facing text since 22 Sep 2026 (CLAUDE.md, the
    closed list)."""
    assert "Dark Army" in card_preparer_brief.BRIEF
    for text in (card_preparer_brief.BRIEF, card_preparer_brief.DESCRIPTION):
        assert "bob" not in text.lower()


def test_the_brief_carries_the_no_tools_sentence_once_and_every_label():
    assert card_preparer_brief.BRIEF.count(card_prepare._NO_TOOLS) == 1
    for label in ("TITLE:", "SUMMARY:", "BENEFICIARY:", "BENEFIT:",
                  "CRITERION:", "INSTRUCTIONS:", "SPECIALISTS:", "FOLDER:"):
        assert label in card_preparer_brief.BRIEF, label
    assert "14" in card_preparer_brief.BRIEF
    assert len(card_preparer_brief.BRIEF) < 4096
    assert card_prepare.brief_ok(card_preparer_brief.BRIEF)


def test_brief_ok_still_accepts_a_copy_carrying_only_the_five_legacy_labels():
    """The stated rule for older briefs: the mode head names the three
    objective labels, so a project copy that never mentions them still
    answers them, and an absent answer is an empty suggestion — never a
    reason to set the copy aside."""
    legacy = (card_preparer_brief.BRIEF
              .replace("BENEFICIARY:", "")
              .replace("BENEFIT:", "")
              .replace("CRITERION:", ""))
    assert "BENEFICIARY:" not in legacy
    assert card_prepare.brief_ok(legacy)
