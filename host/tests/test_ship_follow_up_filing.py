"""A plan's follow-up cards are filed once, by the planning run.

On 23 Sep 2026 two Prep cards ("Confirm and rename the Grok client name",
"End the channel dual-name window") were filed twice: the planning run filed
the plan's `## Out of scope` follow-ups, then the implement run filed them
again. Refine on the second copy found the work already planned or done. The
rule is written, not enforced, so this holds every copy of it in place: the
plan template marks a follow-up, plan mode files it, implement mode never does.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACK = "host/dark_army_menubar/agent_pack/template/.claude"

TEMPLATES = (
    ".claude/skills/ship/templates/plan.md",
    ".agents/skills/ship/templates/plan.md",
    f"{PACK}/skills/ship/templates/plan.md",
)
PLAN_MODES = (
    ".claude/skills/ship/references/plan.md",
    f"{PACK}/skills/ship/references/plan.md",
)
IMPLEMENT_MODES = (
    ".claude/skills/ship/references/implement.md",
    f"{PACK}/skills/ship/references/implement.md",
)
IMPLEMENTER_BRIEFS = (
    ".claude/agents/bc-implementer.md",
    f"{PACK}/agents/{{{{P}}}}-implementer.md",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


@pytest.mark.parametrize("rel", TEMPLATES)
def test_the_plan_template_marks_a_follow_up_under_out_of_scope(rel):
    section = _section(_read(rel), "## Out of scope")
    assert "- Follow-up card:" in section
    assert "planning run files it" in section


@pytest.mark.parametrize("rel", PLAN_MODES)
def test_plan_mode_files_the_follow_ups_before_telling_the_user(rel):
    phase = _section(_read(rel), "## Phase 5: file the plan on the board")
    rule = phase.index("**The plan's follow-ups are filed here and only here.**")
    assert "`Follow-up card:`" in phase[rule:]
    assert rule < phase.index("Then tell the user")


@pytest.mark.parametrize("rel", IMPLEMENT_MODES)
def test_implement_mode_never_refiles_what_the_plan_names(rel):
    text = _read(rel)
    rule = text.index("**Never file what the plan names.**")
    # Stated beside the filing rule it limits, before the no-tool fallback.
    assert text.index("**Out-of-scope rows never drive an iteration.**") < rule
    assert rule < text.index("is not among this session's callable tools")


@pytest.mark.parametrize("rel", IMPLEMENTER_BRIEFS)
def test_the_implementer_reports_only_follow_ups_the_plan_does_not_name(rel):
    assert "FOLLOW-UPS: <out-of-scope work discovered that the plan's Out of scope does not name" in _read(rel)
