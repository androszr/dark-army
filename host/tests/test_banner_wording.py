"""Helper banners keep the terminal look without quoting *Mr. Robot* or the old name.

Every `banners/*.txt` a skill prints — Dark Army's own under `.claude/skills`
and its `.agents/skills` byte mirror, and the shared pack's template and
profiles — is text a person reads, so it names Dark Army and carries no show
catchphrase, organisation or character tic
(`plans/2026-09-22-banner-wording-without-the-show.md`).
"""
import re
from pathlib import Path

import pytest

from dark_army_menubar import pack_render

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
AGENTS_SKILLS = ROOT / ".agents" / "skills"
PACK = ROOT / "host" / "dark_army_menubar" / "agent_pack"
ROOTS = (CLAUDE_SKILLS, AGENTS_SKILLS, PACK)

# Update when a banner is added or removed: a moved directory must not pass the
# denylist by yielding no files at all.
EXPECTED_BANNERS = 27

DENY = re.compile(
    r"fsociety|hello|friend|\be\s*corp\b|evil\s*corp|\bfbi\b|cyber division"
    r"|whoami|nobody\.\s*everybody|perfect or nothing|\bssa\b|mr\.?\s*robot"
    r"|\bbob\b",
    re.I,
)

SPAWN_TABLES = (
    CLAUDE_SKILLS / "ship" / "SKILL.md",
    AGENTS_SKILLS / "ship" / "SKILL.md",
    PACK / "template" / ".claude" / "skills" / "ship" / "SKILL.md",
)


def _banners_under(root: Path) -> list[Path]:
    return sorted(root.rglob("banners/*.txt"))


def _banners() -> list[Path]:
    return sorted(p for root in ROOTS for p in _banners_under(root))


def _hits(text: str) -> list[tuple[int, str]]:
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if DENY.search(line)]


def test_every_banner_is_found():
    for root in ROOTS:
        assert _banners_under(root), f"no banners found under {root}"
    found = _banners()
    assert len(found) == EXPECTED_BANNERS, (
        f"found {len(found)} banners, expected {EXPECTED_BANNERS}; "
        "update EXPECTED_BANNERS when a banner is added or removed"
    )


@pytest.mark.parametrize("path", _banners(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_banner_quotes_the_show_or_the_old_name(path):
    hits = _hits(path.read_text(encoding="utf-8"))
    rel = path.relative_to(ROOT)
    assert not hits, "\n".join(f"{rel}:{n}: {line}" for n, line in hits)


def test_claude_and_agents_banners_are_byte_identical():
    claude = {p.relative_to(CLAUDE_SKILLS) for p in _banners_under(CLAUDE_SKILLS)}
    agents = {p.relative_to(AGENTS_SKILLS) for p in _banners_under(AGENTS_SKILLS)}
    assert claude == agents
    for rel in sorted(claude):
        assert (CLAUDE_SKILLS / rel).read_bytes() == (AGENTS_SKILLS / rel).read_bytes(), rel


def test_local_banner_headers_say_dark_army():
    local = _banners_under(CLAUDE_SKILLS)
    assert len(local) == 9
    for path in local:
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first.rstrip().endswith("DARK ARMY"), f"{path.relative_to(ROOT)}: {first!r}"


def test_pack_banners_keep_their_placeholders():
    for path in _banners_under(PACK):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        assert "{{PROJECT_UPPER}}" in text.splitlines()[0], rel
        assert "{{PROJECT}}" in text, rel


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_rendered_pack_banners_are_clean(profile):
    mapping = pack_render.render(profile, "ab", "sample-app")
    banners = {k: v for k, v in mapping.items() if "/banners/" in k and k.endswith(".txt")}
    assert banners, f"profile {profile} rendered no banners"
    assert any(k.startswith(".agents/skills/") for k in banners), (
        f"profile {profile} rendered no mirrored banner under .agents/skills/"
    )
    for key, data in sorted(banners.items()):
        hits = _hits(data.decode("utf-8"))
        assert not hits, "\n".join(f"{key}:{n}: {line}" for n, line in hits)


def test_spawn_tables_name_no_show_character():
    for path in SPAWN_TABLES:
        assert "fsociety" not in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


def test_the_denylist_bites():
    for bad in (
        "[fsociety // x]",
        "(bob)",
        "BOB-COMPANION",
        "> HELLO, FRIEND_",
        "FBI FIELD CASE FILE",
        "AGENT: Watch  SSA",
        "[e corp // x]",
        "$ whoami",
    ):
        assert DENY.search(bad), bad
    for good in (
        "[dark army // the doors]",
        "(Dark Army)",
        "nobody commits but you.",
        "a plan nobody wrote down is a plan nobody read.",
        "bc-bug-auditor",
        "CASE: #sample-app",
    ):
        assert not DENY.search(good), good
