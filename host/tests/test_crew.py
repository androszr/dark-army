"""Delivery-area partitions and client mirrors; canonical stage names and banners."""
import re
from pathlib import Path

import pytest

from dark_army_daemon import crew, identity

ROOT = Path(__file__).resolve().parents[2]
PANEL_SPECIALISTS = ROOT / "panel" / "Sources" / "BobPanel" / "Specialists.swift"
PHONE_SPECIALISTS = ROOT / "ios" / "BobPhone" / "Specialists.swift"
SKILL = ROOT / ".claude" / "skills" / "ship" / "SKILL.md"
AGENTS = ROOT / ".claude" / "agents"
BANNERS = ROOT / ".claude" / "skills" / "ship" / "banners"


# ── the skill, the agent and the banners ─────────────────────────────────────

def test_the_seventh_agent_exists_and_writes_nothing():
    text = (AGENTS / "bc-security-reviewer.md").read_text(encoding="utf-8")
    tools = re.search(r"^tools: (.+)$", text, re.M)
    assert tools, "the agent declares no tools line"
    named = {t.strip() for t in tools.group(1).split(",")}
    assert named == {"Read", "Glob", "Grep", "Bash"}
    assert not named & {"Edit", "Write", "NotebookEdit"}
    assert "VERDICT:" in text
    assert len(list(AGENTS.glob("*.md"))) == 7


def test_the_skill_spawn_table_names_only_the_chief_of_staff_face():
    text = SKILL.read_text()
    rows = re.findall(r"^\| `(bc-[a-z-]+)` \| ([^|]+?) \|", text, re.M)
    assert {r for r, _ in rows} == set(crew.ROLES) - {"bc-card-preparer"}
    for role, face in rows:
        if role == "bc-planner":
            assert "Overwatch" in face and "chief of staff" in face
        else:
            assert face == "—"

def test_ptys_is_a_pocket_lead():
    from dark_army_daemon import areas
    assert areas.pool_for("pocket") == ("mira", "ptyś")
    assert areas.allocate("pocket", "card", {"mira"}) == "ptyś"

@pytest.mark.parametrize("client", ["panel/Sources/BobPanel", "ios/BobPhone"])
def test_delivery_area_mirror(client):
    from dark_army_daemon import areas
    text = (ROOT / client / "Areas.swift").read_text()
    rows = re.findall(r'Area\(slug: "([a-z]+)", name: "([^"]+)", concept: "([^"]+)", pool: \[(.*?)\]\)', text)
    assert len(rows) == 8
    assert [(a,b,c,tuple(re.findall(r'"([^"]+)"',d))) for a,b,c,d in rows] == [(a.slug,a.name,a.concept,a.pool) for a in areas.AREAS]

def test_delivery_partition():
    from dark_army_daemon import areas
    flat = [c for area in areas.AREAS for c in area.pool]
    assert len(flat) == len(set(flat)) == 19
    assert set(flat) == {n.lower() for n in identity.NAMES if n != "Cipher"}
    assert len(crew.ROLES) == 7
    assert areas.CHIEF_OF_STAFF == "cipher"
    assert all(not set(a.pool).intersection(identity.ART_ONLY) for a in areas.AREAS)


@pytest.mark.parametrize("role", crew.ROLES)
def test_canonical_stage_roster(role):
    assert crew.is_role(role.upper())
    assert (AGENTS / f"{role}.md").is_file()

def test_unknown_stage_is_not_a_role():
    assert not crew.is_role("made-up")
