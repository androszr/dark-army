"""Grep contract: one writer, a pure renderer, the resource is declared."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOST = REPO / "host"
RENDER = HOST / "dark_army_menubar" / "pack_render.py"
INSTALL = HOST / "dark_army_menubar" / "pack_install.py"
LEDGER = HOST / "dark_army_menubar" / "pack_ledger.py"
SETUP = HOST / "setup.py"
RUFF = REPO / "ruff.toml"


_SKIP_PARTS = {".venv", "dist", "build", "__pycache__", ".eggs"}


def _py_files():
    return [
        p for p in HOST.rglob("*.py")
        if p.is_file() and not _SKIP_PARTS & set(p.parts)
    ]


def test_pack_destinations_named_in_exactly_two_files():
    hits = [
        p.relative_to(REPO).as_posix()
        for p in _py_files()
        if "PACK_DESTINATIONS" in p.read_text(encoding="utf-8")
    ]
    assert sorted(hits) == [
        "host/dark_army_menubar/pack_install.py",
        "host/tests/test_agent_pack_contract.py",
    ]


def test_renderer_is_pure():
    text = RENDER.read_text(encoding="utf-8")
    for needle in (
        "write_text", "write_bytes", "os.replace", "shutil.copy",
        "subprocess", "open(",
    ):
        assert needle not in text, needle


def test_setup_declares_the_resource():
    text = SETUP.read_text(encoding="utf-8")
    assert text.count("dark_army_menubar/agent_pack") == 1


def test_ruff_excludes_the_vendored_tree():
    text = RUFF.read_text(encoding="utf-8")
    assert "agent_pack" in text


def test_upstream_is_not_a_runtime_path():
    for path in (INSTALL, RENDER, LEDGER):
        text = path.read_text(encoding="utf-8")
        assert "starter-pack" not in text, path.name
        assert "/Users/" not in text, path.name
        assert "/home/" not in text, path.name


def test_vendored_tree_has_no_home_paths():
    root = HOST / "dark_army_menubar" / "agent_pack"
    assert (root / "PORTED-FROM.md").is_file()
    assert (root / "template" / ".claude" / "skills" / "ship").is_dir()
    for name in ("common", "plan", "implement"):
        assert (root / "template" / ".claude" / "skills" / "ship" / "references" / f"{name}.md").is_file()
    hits = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b"/Users/" in blob or b"/home/" in blob:
            hits.append(path.relative_to(REPO).as_posix())
    assert hits == []


def test_ported_from_names_the_commit():
    text = (HOST / "dark_army_menubar" / "agent_pack" / "PORTED-FROM.md").read_text(
        encoding="utf-8")
    assert "b114f221ba8d1adfa51dbb2a4cbe507d9531fc06" in text
    assert "/Users/" not in text
    assert "/home/" not in text


def test_the_read_only_templates_delegate_no_read_and_no_write():
    """The pack's verifier and bug-auditor briefs are read-only roles: they
    edit no file and read nothing through a summary, so neither may tell the
    role to Bulk-read or Code-write through the shunt helpers. The exemption
    (`exempt.py on`, read the file whole) is the one shunt script they name;
    the writer templates keep the full section."""
    agents = HOST / "dark_army_menubar" / "agent_pack" / "template" / ".claude" / "agents"
    for name in ("{{P}}-verifier.md", "{{P}}-bug-auditor.md"):
        text = (agents / name).read_text(encoding="utf-8")
        assert "exempt.py on" in text, name
        for word in ("Bulk-read", "Code-write", "bulk_read.py", "code_write.py"):
            assert word not in text, (name, word)
    for name in ("{{P}}-planner.md", "{{P}}-implementer.md"):
        text = (agents / name).read_text(encoding="utf-8")
        assert "bulk_read.py" in text and "code_write.py" in text, name


def test_starter_gitignore_ships_beside_the_template():
    """The base ignore list is `agent_pack/gitignore.txt`. A `.gitignore`
    under `template/` would be rendered as a pack file and would act on this
    repository's own template tree."""
    root = HOST / "dark_army_menubar" / "agent_pack"
    assert (root / "gitignore.txt").is_file()


def test_no_gitignore_inside_the_template():
    root = HOST / "dark_army_menubar" / "agent_pack"
    assert not (root / "template" / ".gitignore").exists()


GUARD_ROWS = (
    "Read(~/.dark-army/api-token)",
    "Read(~/.dark-army/relay.json)",
    "Read(~/.dark-army/devices.json)",
    "Read(~/.dark-army/grok-bot.json)",
    "Read(~/.dark-army/grok-bot.secrets)",
    "Read(~/.dark-army/grok-bot-mcp-token)",
    "Read(~/.bob-companion/**)",
    "Read(**/.dark-army/key)",
    "Read(**/.bob-companion/key)",
    "Bash(cat ~/.dark-army/relay.json*)",
    "Bash(cat ~/.dark-army/devices.json*)",
    "Bash(cat ~/.dark-army/grok-bot*)",
    "Bash(cat .dark-army/key*)",
    # The script Dark Army runs, unsandboxed, when a card worktree is made.
    "Edit(**/.dark-army/worktree-setup.sh)",
)


def test_the_guard_rows_are_pinned_in_the_template_and_this_checkout():
    """The same deny rows in the pack template and this checkout's own
    settings (never rendered by the pack); a Bash row uses the CLI's `*`
    wildcard, never `:*` after a path, which would match nothing."""
    import json
    template = HOST / "dark_army_menubar" / "agent_pack" / "template" / ".claude" / "settings.json"
    template_deny = json.loads(template.read_text(encoding="utf-8").replace(
        "{{ALLOW_ROWS}}", ""))["permissions"]["deny"]
    own_deny = json.loads((REPO / ".claude" / "settings.json").read_text(
        encoding="utf-8"))["permissions"]["deny"]
    for row in GUARD_ROWS:
        assert row in template_deny
        assert row in own_deny
