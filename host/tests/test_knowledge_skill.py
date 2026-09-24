# host/tests/test_knowledge_skill.py
"""The `/knowledge` skill and its question catalogue: four copies of one text,
and a catalogue whose structure is parsed rather than eyeballed.

`test_review_skill.py`'s job for the other shipped skill, with the canonical
file the other way round: the **template** pair is the text every project
receives, and this repo's four copies must equal it byte for byte — a drift
here would mean the text we ship and the text we run are two different texts.
`tools/sync_knowledge_skill.py` is the repair.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CANONICAL = (REPO / "host/dark_army_menubar/agent_pack/template"
             / ".claude/skills/knowledge")
COPIES = {
    "SKILL.md": [
        REPO / ".claude/skills/knowledge/SKILL.md",
        REPO / ".agents/skills/knowledge/SKILL.md",
    ],
    "questions.md": [
        REPO / ".claude/skills/knowledge/questions.md",
        REPO / ".agents/skills/knowledge/questions.md",
    ],
}

#: The store's own normalisation. A catalogue key that does not survive it
#: would be silently rewritten on the way in, and the answer would come back
#: under a name the skill cannot find again.
KEY_RE = re.compile(r"^[a-z0-9._-]{1,64}$")
_SECTION = re.compile(r"^## [A-Z]\. ", re.M)


def _skill() -> str:
    return (CANONICAL / "SKILL.md").read_text(encoding="utf-8")


def _catalogue() -> str:
    return (CANONICAL / "questions.md").read_text(encoding="utf-8")


def _sections() -> list[str]:
    text = _catalogue()
    starts = [m.start() for m in _SECTION.finditer(text)]
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]


# --- one text, four copies -----------------------------------------------------


@pytest.mark.parametrize(
    "name,copy",
    [(name, copy) for name, copies in COPIES.items() for copy in copies],
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_every_copy_is_byte_identical_to_the_shipped_file(name, copy):
    assert copy.is_file(), (
        f"{copy} is missing — run tools/sync_knowledge_skill.py")
    assert copy.read_bytes() == (CANONICAL / name).read_bytes(), (
        f"{copy} has drifted — run tools/sync_knowledge_skill.py")


@pytest.mark.parametrize("name", sorted(COPIES))
def test_neither_canonical_file_carries_a_placeholder(name):
    """Placeholder-free, so `pack_render.substitute` has nothing to resolve and
    a second project receives exactly this text."""
    assert "{{" not in (CANONICAL / name).read_text(encoding="utf-8")


# --- the skill -----------------------------------------------------------------


def test_the_skill_names_both_tools_and_the_catalogue():
    text = _skill()
    assert "dark_army_knowledge_read" in text
    assert "dark_army_knowledge_write" in text
    assert ".claude/skills/knowledge/questions.md" in text


def test_the_skill_declares_its_frontmatter_name():
    text = _skill()
    assert text.startswith("---\n")
    assert re.search(r"^name: knowledge$", text, re.M)
    assert re.search(r"^description: ", text, re.M)
    assert "/knowledge" in text


def test_the_skill_states_the_three_question_ceiling_and_the_stop():
    text = " ".join(_skill().split())
    assert "**three at most**" in text
    assert "At most three questions per run" in text


def test_the_skill_says_what_to_do_where_the_tools_are_absent():
    """Claude-only today: the `.agents` mirror exists regardless, and a skill
    that asked the questions anyway would cost a person's time for nothing."""
    text = " ".join(_skill().split())
    assert "Claude-only" in text
    assert "not available in this session" in text


def test_the_skill_warns_that_the_agents_mirror_does_not_follow_an_edit():
    """Seed-once freezes *both* copies, so a project that reworks its `.claude`
    catalogue — which the header invites — leaves Codex and Grok on different
    questions under different keys. The mirror keys stay in `SEED_ONCE_KEYS`
    (`_unlink_strays` sparing an edited mirror depends on it), so the cost is
    paid in words, here, where the person editing will read it."""
    text = " ".join(_skill().split())
    assert ".agents/skills/knowledge/" in text
    assert "editing one does not update the other" in text
    assert "delete the `.agents` copy" in text


def test_the_skill_warns_that_seeding_happens_once():
    """The single largest cost of the chosen distribution, said where the
    person who is surprised by it will be reading."""
    text = " ".join(_skill().split())
    assert "**once**" in text
    assert "delete the file" in text


# --- the catalogue's structure -------------------------------------------------


def test_the_catalogue_declares_at_least_six_lettered_sections():
    assert len(_sections()) >= 6


@pytest.mark.parametrize("index", range(10))
def test_each_section_has_one_key_one_prompt_and_three_options(index):
    sections = _sections()
    if index >= len(sections):
        pytest.skip("fewer sections than the parametrisation covers")
    section = sections[index]
    heading = section.splitlines()[0]
    keys = re.findall(r"^key: `([^`]+)`$", section, re.M)
    assert len(keys) == 1, f"{heading}: expected exactly one key: line"
    prompts = [line for line in section.splitlines() if line.startswith("> ")]
    assert len(prompts) == 1, f"{heading}: expected exactly one > prompt"
    options = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(options) >= 3, f"{heading}: expected at least three options"


def test_every_key_is_unique_and_survives_the_stores_normalisation():
    keys = re.findall(r"^key: `([^`]+)`$", _catalogue(), re.M)
    assert len(keys) == len(set(keys)), "a duplicate key would overwrite itself"
    for key in keys:
        assert KEY_RE.fullmatch(key), key


def test_the_keys_match_the_stores_own_normalise_function():
    """Not a second copy of the rule: the store's function is called."""
    from dark_army_daemon import knowledge_store as ks

    for key in re.findall(r"^key: `([^`]+)`$", _catalogue(), re.M):
        assert ks.normalise_key(key) == key


def test_the_catalogue_covers_the_subjects_the_plan_names():
    keys = set(re.findall(r"^key: `([^`]+)`$", _catalogue(), re.M))
    for required in ("purpose", "audience", "usage", "constraints",
                     "architecture", "deliberate_omissions", "success",
                     "reverted"):
        assert required in keys, required


def test_the_catalogue_header_states_the_contract_the_skill_relies_on():
    header = _catalogue().split("\n---\n", 1)[0]
    assert "at most three" in header.lower()
    assert "one at a time" in header.lower()
    assert "tree" in header.lower()
