"""The `/review` skill: one canonical text, two byte copies, a fixed contract.

`.claude/skills/review/SKILL.md` is the master. `.agents/skills/review/SKILL.md`
(Codex and Grok) is a byte-identical twin and **fails** here when it drifts.
`~/.claude/skills/review/SKILL.md` is the personal mirror two other repos read;
it is **reported**, never enforced, because a machine without it is not out of
step. `tools/sync_review_skill.py` repairs both.

The content contract — the target question in three forms, the four grades,
the SHIP/STOP verdict, the card rules and the CLI form of every GitNexus call —
is pinned as text: the dialogs themselves are drawn by three assistant runtimes
this suite cannot reach.
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from pathlib import Path

import pytest

from dark_army_daemon import session_stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import sync_review_skill  # noqa: E402  (tools/ is not a package)

CANONICAL = ROOT / ".claude/skills/review/SKILL.md"
TWIN = ROOT / ".agents/skills/review/SKILL.md"
MANIFEST = ROOT / ".agents/skills/review/agents/openai.yaml"
BUG_AUDITOR = ROOT / ".claude/agents/bc-bug-auditor.md"

GRADE_LINES = [
    "**BLOCK** — must be fixed before this ships.",
    "**FIX** — should be fixed; shipping without it is still defensible.",
    "**WARN** — worth knowing; a risk accepted knowingly.",
    "**NOTE** — informational.",
]

_ACTIONS_LINE = re.compile(r"<!--\s*bob-actions:\s*(.*?)\s*-->")


def _text() -> str:
    assert CANONICAL.is_file(), f"missing canonical skill: {CANONICAL}"
    return CANONICAL.read_text(encoding="utf-8")


def test_twin_is_byte_identical():
    assert TWIN.is_file(), (
        f"{TWIN} is missing; run python3 tools/sync_review_skill.py")
    assert TWIN.read_bytes() == CANONICAL.read_bytes(), (
        ".agents/skills/review/SKILL.md has drifted from the canonical "
        ".claude/skills/review/SKILL.md; run python3 tools/sync_review_skill.py")


def test_twin_manifest_names_the_skill():
    assert MANIFEST.is_file()
    text = MANIFEST.read_text(encoding="utf-8")
    for key in ("display_name:", "short_description:", "default_prompt:"):
        assert key in text
    assert text.count("$review") == 1


def test_personal_mirror_is_reported_not_enforced():
    # The real home, read-only, on purpose: `paths._home()` is redirected under
    # pytest and would never see the developer's mirror.
    personal = Path.home() / ".claude/skills/review/SKILL.md"
    if not personal.is_file():
        return
    if personal.read_bytes() != CANONICAL.read_bytes():
        warnings.warn(
            "personal /review mirror is behind; run tools/sync_review_skill.py",
            UserWarning,
        )


def test_personal_mirror_drift_warns_and_passes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    mirror = home / ".claude/skills/review/SKILL.md"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("older text\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.warns(UserWarning, match="tools/sync_review_skill.py"):
        test_personal_mirror_is_reported_not_enforced()


def test_frontmatter_and_kept_sections():
    text = _text()
    assert text.startswith("---\nname: review\n")
    for needle in (
        "## Step 0",
        "## Step 1: choose the target",
        "## Step 8",
        ".claude/review.md",
        "--repair-fts",
        "Backlog only",
        "At most 8 cards",
        "Never file a card unasked",
        "Nothing below `likely`",
        "list_repos",
    ):
        assert needle in text, needle
    assert "`summary` is not the title again" in text


def test_grade_set_is_fixed():
    text = _text()
    positions = []
    for line in GRADE_LINES:
        assert text.count(line) == 1, line
        positions.append(text.index(line))
    assert positions == sorted(positions), "grades are not in BLOCK/FIX/WARN/NOTE order"
    # Each definition is its own bullet, and nothing but the bullet marker
    # precedes it on the line.
    for line in GRADE_LINES:
        assert f"\n- {line}\n" in text, line


def test_verdict_rule():
    text = _text()
    assert "VERDICT: SHIP" in text
    assert "VERDICT: STOP" in text
    assert "Any BLOCK → `STOP` regardless of count" in text
    assert text.count("cannot be outvoted by a low total") == 1
    # The auditor wraps the sentence across a line break; compare the words.
    ancestor = " ".join(BUG_AUDITOR.read_text(encoding="utf-8").split())
    assert ancestor.count("cannot be outvoted by a low total") == 1


def test_three_question_forms():
    text = _text()
    for needle in (
        "AskUserQuestion",
        "ask_user_question",
        "numbered list",
        "<!-- bob-tldr:",
        "<!-- bob-actions:",
        "ends the turn",
    ):
        assert needle in text, needle
    offers = _ACTIONS_LINE.findall(text)
    assert offers, "no bob-actions line to check"
    for raw in offers:
        labels = session_stats._parse_actions(raw)
        assert labels, f"bob-actions line does not parse: {raw!r}"
        assert len(labels) <= session_stats.MAX_ACTIONS
        for label in labels:
            assert len(label) <= session_stats.MAX_ACTION_CHARS, label
        # The parser refuses the whole row on one over-long label; make sure
        # nothing was silently dropped from what the skill offered.
        assert len(labels) == len([p for p in raw.split("|") if p.strip()])


def test_gitnexus_calls_have_a_cli_form():
    text = _text()
    assert "run.cjs detect-changes" in text
    assert "run.cjs impact" in text
    assert "gitnexus-pr-review" not in text
    assert "gitnexus-impact-analysis" in text


def test_card_call_is_the_bare_tool_name():
    text = _text()
    assert "mcp__dark-army__dark_army_add_card" not in text
    assert "mcp__bob__bob_add_card" not in text
    assert text.count("dark_army_add_card") >= 2
    assert "dark-army-board" in text


def test_sync_is_idempotent_and_atomic(tmp_path):
    canonical = tmp_path / "canonical/SKILL.md"
    canonical.parent.mkdir()
    canonical.write_text("---\nname: review\n---\nbody\n", encoding="utf-8")
    twin = tmp_path / "twin/SKILL.md"
    personal = tmp_path / "home/.claude/skills/review/SKILL.md"

    first = sync_review_skill.sync(canonical, [twin, personal], write=True)
    assert first == {twin: "updated", personal: "updated"}
    assert twin.read_bytes() == canonical.read_bytes()
    assert personal.read_bytes() == canonical.read_bytes()
    assert os.stat(twin).st_mode & 0o777 == 0o644
    stamps = (twin.stat().st_mtime_ns, personal.stat().st_mtime_ns)

    second = sync_review_skill.sync(canonical, [twin, personal], write=True)
    assert second == {twin: "in step", personal: "in step"}
    assert (twin.stat().st_mtime_ns, personal.stat().st_mtime_ns) == stamps

    twin.write_text("hand edit\n", encoding="utf-8")
    os.chmod(twin, 0o600)
    dry = sync_review_skill.sync(canonical, [twin, personal], write=False)
    assert dry == {twin: "updated", personal: "in step"}
    assert twin.read_text(encoding="utf-8") == "hand edit\n"

    repaired = sync_review_skill.sync(canonical, [twin, personal], write=True)
    assert repaired == {twin: "updated", personal: "in step"}
    assert twin.read_bytes() == canonical.read_bytes()
    assert os.stat(twin).st_mode & 0o777 == 0o600, "mode is preserved on rewrite"
    assert not [p for p in twin.parent.iterdir() if p.name != "SKILL.md"], (
        "a temporary file was left beside the target")

    personal.unlink()
    missing = sync_review_skill.sync(canonical, [twin, personal], write=False)
    assert missing == {twin: "in step", personal: "missing"}


def test_sync_copies_a_crlf_canonical_byte_for_byte(tmp_path):
    # A text round-trip folds CRLF to LF, leaving a twin that sync calls
    # "in step" while the byte comparison above fails forever.
    canonical = tmp_path / "canonical.md"
    canonical.write_bytes(b"---\r\nname: review\r\n---\r\nbody\r\n")
    twin = tmp_path / "twin.md"

    assert sync_review_skill.sync(canonical, [twin], write=True) == {twin: "updated"}
    assert twin.read_bytes() == canonical.read_bytes()
    assert b"\r\n" in twin.read_bytes()
    assert sync_review_skill.sync(canonical, [twin], write=False) == {twin: "in step"}


def test_check_exit_codes(tmp_path, monkeypatch, capsys):
    canonical = tmp_path / "canonical.md"
    canonical.write_text("the text\n", encoding="utf-8")
    twin = tmp_path / "twin.md"
    personal = tmp_path / "personal.md"
    monkeypatch.setattr(sync_review_skill, "CANONICAL", canonical)
    monkeypatch.setattr(sync_review_skill, "TWIN", twin)
    monkeypatch.setattr(sync_review_skill, "PERSONAL", personal)

    # Twin missing → out of step.
    assert sync_review_skill.main(["--check"]) == 1
    assert twin.exists() is False, "--check must not write"

    twin.write_text("the text\n", encoding="utf-8")
    # Twin in step, personal missing → 0, and the mirror is mentioned.
    assert sync_review_skill.main(["--check"]) == 0
    assert "personal mirror" in capsys.readouterr().out

    personal.write_text("older\n", encoding="utf-8")
    assert sync_review_skill.main(["--check"]) == 0
    assert personal.read_text(encoding="utf-8") == "older\n"

    twin.write_text("drifted\n", encoding="utf-8")
    assert sync_review_skill.main(["--check"]) == 1
    assert sync_review_skill.main(["--check", "--no-personal"]) == 1

    canonical.unlink()
    assert sync_review_skill.main(["--check"]) == 2


# --- the Review section's handoff (docs/review-runs.md) -------------------------

TEMPLATE = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/review/SKILL.md"


@pytest.mark.parametrize("path", [CANONICAL, TEMPLATE], ids=["canonical", "template"])
def test_both_texts_carry_the_dark_army_handoff(path):
    text = path.read_text(encoding="utf-8")
    assert text.count("## When Dark Army runs it") == 1
    assert text.index("## When Dark Army runs it") < text.index("## Ending the turn")
    for needle in ("/review remote", "findings.md", "picks.json", "steps.md",
                   "STEP ", "DONE", "@{u}", "pty broker"):
        assert needle in text, needle
    flat = " ".join(text.split())
    assert "authorised steps are the only ones" in flat
    assert "a step the block does not list is a step the person did not ask for" in flat


def test_the_template_keeps_the_grade_list_once_too():
    text = TEMPLATE.read_text(encoding="utf-8")
    for line in GRADE_LINES:
        assert text.count(line) == 1, line
