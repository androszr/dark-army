"""The shunt skill: one text in the pack template, two byte copies in Dark
Army's own trees, and the sentences the skill must carry.

The template under `host/dark_army_menubar/agent_pack/template/.claude/
skills/shunt/` is the master — every enrolled project receives it through the
pack. `.claude/skills/shunt/` (Claude) and `.agents/skills/shunt/` (Codex and
Grok, plus `agents/openai.yaml`) are byte copies written by
`tools/sync_shunt_skill.py`, and this suite **fails** when either drifts.
`test_review_skill.py`'s shape.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dark_army_menubar import pack_render

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import sync_shunt_skill  # noqa: E402  (tools/ is not a package)

TEMPLATE = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/shunt"
CLAUDE = ROOT / ".claude/skills/shunt"
AGENTS = ROOT / ".agents/skills/shunt"
FILES = ("SKILL.md", "bulk_read.py", "code_write.py", "exempt.py", "workers.json")


def test_the_template_carries_exactly_the_five_files():
    assert sorted(p.name for p in TEMPLATE.iterdir() if p.is_file()) == sorted(FILES)
    assert tuple(sync_shunt_skill.FILES) == FILES


def test_both_local_copies_are_byte_identical_to_the_template():
    for name in FILES:
        master = (TEMPLATE / name).read_bytes()
        for tree in (CLAUDE, AGENTS):
            copy = tree / name
            assert copy.is_file(), f"{copy} is missing; run python3 tools/sync_shunt_skill.py"
            assert copy.read_bytes() == master, (
                f"{copy} has drifted from the template; run python3 tools/sync_shunt_skill.py")


def test_the_agents_manifest_is_what_the_pack_would_write():
    manifest = AGENTS / "agents" / "openai.yaml"
    assert manifest.is_file()
    expected = pack_render.openai_yaml((TEMPLATE / "SKILL.md").read_bytes(), "shunt")
    assert manifest.read_bytes() == expected
    assert manifest.read_text(encoding="utf-8").count("$shunt") == 1


def test_no_copy_carries_a_placeholder():
    for name in FILES:
        text = (TEMPLATE / name).read_text(encoding="utf-8")
        assert not pack_render._PLACEHOLDER.search(text), name


def test_the_skill_names_the_two_commands_the_exemption_and_the_never_rule():
    raw = (TEMPLATE / "SKILL.md").read_text(encoding="utf-8")
    assert raw.startswith("---\nname: shunt\n")
    text = " ".join(raw.split())
    for needle in (
        "python3 .claude/skills/shunt/bulk_read.py --question",
        "python3 .claude/skills/shunt/code_write.py --spec",
        "--reference",
        "python3 .claude/skills/shunt/exempt.py on",
        "python3 .claude/skills/shunt/exempt.py off",
        "## When never",
        "**An edit.**",
        "**Debugging**",
        "**review**",
        "350 lines",
        "env.BOB_SHUNT_MIN_LINES",
        "BOB_SHUNT_WORKER_MODEL",
        "10–30 seconds",
        "never send a file under the threshold",
        "No file content, question or spec is ever written",
        # Dark Army's own checkout is never installed into: no Worker row
        # reaches it, so the shipped table and the env override are its two.
        "Dark Army's own checkout is never installed into",
        "the shipped table and `BOB_SHUNT_WORKER_MODEL`",
    ):
        assert needle in text, needle
    # A reviewer reads by itself: the sentence the briefs repeat.
    assert "never through a summary" in text


def test_sync_check_passes_on_this_tree():
    assert sync_shunt_skill.main(["--check"]) == 0


def test_sync_is_idempotent_and_atomic(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    for name in FILES:
        (template / name).write_bytes(f"# {name}\n".encode())
    (template / "SKILL.md").write_bytes(b"---\nname: shunt\ndescription: Keep it out.\n---\nbody\n")
    claude = tmp_path / "claude"
    agents = tmp_path / "agents"

    first = sync_shunt_skill.sync(template, claude, agents, write=True)
    assert set(first.values()) == {"updated"}
    assert len(first) == len(FILES) * 2 + 1
    assert (agents / "agents" / "openai.yaml").read_bytes() == pack_render.openai_yaml(
        (template / "SKILL.md").read_bytes(), "shunt")
    assert os.stat(claude / "bulk_read.py").st_mode & 0o777 == 0o644
    stamps = {p: p.stat().st_mtime_ns for p in first}

    second = sync_shunt_skill.sync(template, claude, agents, write=True)
    assert set(second.values()) == {"in step"}
    assert {p: p.stat().st_mtime_ns for p in second} == stamps

    (claude / "exempt.py").write_bytes(b"hand edit\n")
    os.chmod(claude / "exempt.py", 0o600)
    dry = sync_shunt_skill.sync(template, claude, agents, write=False)
    assert dry[claude / "exempt.py"] == "updated"
    assert (claude / "exempt.py").read_bytes() == b"hand edit\n"
    repaired = sync_shunt_skill.sync(template, claude, agents, write=True)
    assert repaired[claude / "exempt.py"] == "updated"
    assert (claude / "exempt.py").read_bytes() == (template / "exempt.py").read_bytes()
    assert os.stat(claude / "exempt.py").st_mode & 0o777 == 0o600
    assert not [p for p in claude.iterdir() if p.name not in FILES], "a temp file was left behind"

    (agents / "workers.json").unlink()
    missing = sync_shunt_skill.sync(template, claude, agents, write=False)
    assert missing[agents / "workers.json"] == "missing"


def test_check_exit_codes(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    for name in FILES:
        (template / name).write_bytes(b"x\n")
    (template / "SKILL.md").write_bytes(b"---\nname: shunt\ndescription: d\n---\n")
    claude = tmp_path / "claude"
    agents = tmp_path / "agents"
    monkeypatch.setattr(sync_shunt_skill, "TEMPLATE", template)
    monkeypatch.setattr(sync_shunt_skill, "CLAUDE", claude)
    monkeypatch.setattr(sync_shunt_skill, "AGENTS", agents)
    assert sync_shunt_skill.main(["--check"]) == 1
    assert not claude.exists(), "--check must not write"
    sync_shunt_skill.sync(template, claude, agents, write=True)
    assert sync_shunt_skill.main(["--check"]) == 0
    (agents / "SKILL.md").write_bytes(b"drifted\n")
    assert sync_shunt_skill.main(["--check"]) == 1
    (template / "exempt.py").unlink()
    assert sync_shunt_skill.main(["--check"]) == 2
