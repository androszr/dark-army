"""The security-audit skill: Cloudflare's files vendored pristine, Dark Army's
adapter beside them, two byte copies in Dark Army's own trees.

The template under `host/dark_army_menubar/agent_pack/template/.claude/skills/
security-audit/` is the master. `upstream/` is Cloudflare's, byte for byte, and
`upstream-pin.json` holds a SHA-256 for each file, so an accidental edit fails
here and names the file. `.claude/skills/security-audit/` and
`.agents/skills/security-audit/` are written by
`tools/sync_security_audit_skill.py`. `test_shunt_skill.py`'s shape.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dark_army_menubar import pack_render

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = ROOT / "host/dark_army_menubar/agent_pack/template"
TEMPLATE = TEMPLATE_ROOT / ".claude/skills/security-audit"
UPSTREAM = TEMPLATE / "upstream"
PIN = TEMPLATE / "upstream-pin.json"
CLAUDE = ROOT / ".claude/skills/security-audit"
AGENTS = ROOT / ".agents/skills/security-audit"
UPDATE = "security-audit-update"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("security_audit_upstream_under_test", ROOT / "tools/security_audit_upstream.py")
sync = _load("sync_security_audit_skill_under_test", ROOT / "tools/sync_security_audit_skill.py")


def _tree(base: Path) -> dict[str, bytes]:
    return {p.relative_to(base).as_posix(): p.read_bytes()
            for p in sorted(base.rglob("*")) if p.is_file()}


def test_the_template_carries_exactly_the_adapter_the_pin_and_21_files():
    top = sorted(p.name for p in TEMPLATE.iterdir())
    assert top == ["SKILL.md", "upstream", "upstream-pin.json"]
    assert len([p for p in UPSTREAM.iterdir() if p.is_file()]) == 21
    assert not [p for p in UPSTREAM.rglob("*") if p.is_dir()]


def test_every_pinned_digest_matches_and_nothing_is_unpinned():
    assert tool.pin_problems(UPSTREAM, PIN) == []
    assert tool.cmd_pin_check(UPSTREAM, PIN) == 0


def test_the_pin_names_cloudflare_and_a_full_commit():
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", pin["commit"])
    assert pin["repository"] == "https://github.com/cloudflare/security-audit-skill"
    assert pin["licence"] == "MIT"
    assert len(pin["files"]) == 21
    assert PIN.read_text(encoding="utf-8").endswith("}\n")


def test_one_byte_of_drift_in_a_pinned_file_is_named(tmp_path):
    upstream = tmp_path / "upstream"
    shutil.copytree(UPSTREAM, upstream)
    target = upstream / "HUNTING.md"
    target.write_bytes(target.read_bytes() + b" ")
    assert tool.pin_problems(upstream, PIN) == ["changed: HUNTING.md"]
    assert tool.cmd_pin_check(upstream, PIN) == 1


def test_a_stray_unpinned_file_fails_the_pin(tmp_path):
    upstream = tmp_path / "upstream"
    shutil.copytree(UPSTREAM, upstream)
    (upstream / "NOTES.md").write_text("mine", encoding="utf-8")
    assert tool.pin_problems(upstream, PIN) == ["unpinned: NOTES.md"]


def test_a_missing_pinned_file_is_named(tmp_path):
    upstream = tmp_path / "upstream"
    shutil.copytree(UPSTREAM, upstream)
    (upstream / "LICENSE").unlink()
    assert tool.pin_problems(upstream, PIN) == ["missing: LICENSE"]


def test_the_vendored_skill_and_licence_are_cloudflares():
    head = (UPSTREAM / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert re.search(r"^name:\s*security-audit\s*$", head, re.M)
    licence = (UPSTREAM / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in licence and "Cloudflare" in licence


def test_nothing_vendored_trips_the_pack_scans():
    for path in [*UPSTREAM.iterdir(), TEMPLATE / "SKILL.md"]:
        text = path.read_text(encoding="utf-8")
        assert not pack_render._PLACEHOLDER.search(text), path.name
        assert "/Users/" not in text and "/home/" not in text, path.name
        assert not re.search(r"\bbob\b", text, re.I), path.name


def test_the_adapter_carries_the_phrases_it_promises():
    raw = (TEMPLATE / "SKILL.md").read_text(encoding="utf-8")
    assert raw.startswith("---\nname: security-audit\n")
    for phrase in ("upstream/SKILL.md", "security-audit/run-", "git check-ignore",
                   "dark_army_add_card", "needs_validation",
                   "validators_unavailable_no_node", "never inside", "Dark Army",
                   "command -v node", "What this overlay assumes about upstream"):
        assert phrase in raw, phrase
    flat = " ".join(raw.split()).replace("Dark Army", "")
    assert not re.search(r"\bDA\b|\bArmy\b", flat)
    short = pack_render.openai_yaml(raw.encode(), "security-audit").decode()
    assert "Dark Army's rules." in short  # the first sentence fits whole


def test_both_local_copies_are_byte_identical_to_the_template():
    master = _tree(TEMPLATE)
    for tree in (CLAUDE, AGENTS):
        copy = {k: v for k, v in _tree(tree).items() if k != "agents/openai.yaml"}
        assert copy == master, f"{tree} drifted; run python3 tools/sync_security_audit_skill.py"


def test_the_agents_manifest_is_what_the_pack_would_write():
    manifest = AGENTS / "agents/openai.yaml"
    expected = pack_render.openai_yaml((TEMPLATE / "SKILL.md").read_bytes(), "security-audit")
    assert manifest.read_bytes() == expected
    assert not (CLAUDE / "agents").exists()


def test_the_update_skill_is_here_and_not_in_the_template():
    claude = ROOT / ".claude/skills" / UPDATE / "SKILL.md"
    agents = ROOT / ".agents/skills" / UPDATE
    assert claude.is_file()
    assert (agents / "SKILL.md").read_bytes() == claude.read_bytes()
    assert (agents / "agents/openai.yaml").read_bytes() == pack_render.openai_yaml(
        claude.read_bytes(), UPDATE)
    assert not (TEMPLATE_ROOT / ".claude/skills" / UPDATE).exists()
    raw = claude.read_text(encoding="utf-8")
    assert raw.startswith(f"---\nname: {UPDATE}\n")
    assert not pack_render._PLACEHOLDER.search(raw)
    assert "explicit yes" in raw and "Never commit" in raw


def test_sync_check_passes_on_this_tree():
    assert sync.main(["--check"]) == 0


def _fake_tree(tmp_path: Path):
    template = tmp_path / "template"
    shutil.copytree(TEMPLATE, template)
    claude, agents = tmp_path / "c", tmp_path / "a"
    return template, claude, agents


def test_sync_is_idempotent_and_writes_the_manifest(tmp_path):
    template, claude, agents = _fake_tree(tmp_path)
    first = sync.sync(template, claude, agents, write=True)
    assert set(first.values()) == {"updated"}
    again = sync.sync(template, claude, agents, write=True)
    assert set(again.values()) == {"in step"}
    assert (agents / "agents/openai.yaml").is_file()
    assert _tree(claude) == _tree(template)


def test_sync_reports_and_removes_a_stray(tmp_path):
    template, claude, agents = _fake_tree(tmp_path)
    sync.sync(template, claude, agents, write=True)
    stray = claude / "upstream" / "GONE.md"
    stray.write_text("old", encoding="utf-8")
    check = sync.sync(template, claude, agents, write=False)
    assert check[stray] == "stray"
    sync.sync(template, claude, agents, write=True)
    assert not stray.exists()


def test_sync_check_reports_a_drifted_copy_without_writing(tmp_path):
    template, claude, agents = _fake_tree(tmp_path)
    sync.sync(template, claude, agents, write=True)
    victim = agents / "SKILL.md"
    victim.write_text("drift", encoding="utf-8")
    result = sync.sync(template, claude, agents, write=False)
    assert result[victim] == "updated"
    assert victim.read_text(encoding="utf-8") == "drift"


def test_sync_leaves_no_temporary_sibling(tmp_path):
    template, claude, agents = _fake_tree(tmp_path)
    sync.sync(template, claude, agents, write=True)
    names = [p.name for p in claude.rglob("*") if p.is_file()]
    assert not [n for n in names if n.startswith("tmp")]


def _allow_rows(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]


_V = "host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/upstream/"
_EXACT_TEST_ROW = (f"Bash(node --test {_V}validate-findings.test.cjs "
                   f"{_V}validate-coverage-ledger.test.cjs)")


def test_the_template_allows_exactly_the_two_validators():
    # The template holds a {{ALLOW_ROWS}} placeholder, so it is not JSON yet.
    raw = (TEMPLATE_ROOT / ".claude/settings.json").read_text(encoding="utf-8")
    rows = [r for r in re.findall(r'^\s*"(Bash\(node[^"]*)",?\s*$', raw, re.M)]
    assert sorted(rows) == [
        "Bash(node .claude/skills/security-audit/upstream/validate-coverage-ledger.cjs:*)",
        "Bash(node .claude/skills/security-audit/upstream/validate-findings.cjs:*)"]


def test_this_checkout_allows_the_validators_and_one_exact_test_row_only():
    rows = [r for r in _allow_rows(ROOT / ".claude/settings.json") if r.startswith("Bash(node")]
    assert sorted(rows) == sorted([
        "Bash(node .claude/skills/security-audit/upstream/validate-coverage-ledger.cjs:*)",
        "Bash(node .claude/skills/security-audit/upstream/validate-findings.cjs:*)",
        _EXACT_TEST_ROW])
    assert "validate-findings.test.cjs" in (ROOT / ".claude/skills" / UPDATE / "SKILL.md").read_text(
        encoding="utf-8")
    assert _EXACT_TEST_ROW[len("Bash("):-1] in (ROOT / ".claude/skills" / UPDATE / "SKILL.md").read_text(
        encoding="utf-8")


def test_no_row_lets_apply_or_pin_write_run_unprompted():
    rows = [r for r in _allow_rows(ROOT / ".claude/settings.json") if "security_audit_upstream" in r]
    assert sorted(rows) == sorted([
        "Bash(python3 tools/security_audit_upstream.py check)",
        "Bash(python3 tools/security_audit_upstream.py pin --check)",
        "Bash(python3 tools/security_audit_upstream.py fetch:*)",
        "Bash(python3 tools/security_audit_upstream.py diff:*)"])
    for row in rows:
        assert "apply" not in row and "--write" not in row
        assert not row.endswith("upstream.py:*)")
    skill = (ROOT / ".claude/skills" / UPDATE / "SKILL.md").read_text(encoding="utf-8")
    assert "deliberately triggers a permission prompt" in skill


def _adapter_check_ignore_argv() -> list[str]:
    raw = (TEMPLATE / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"`(git check-ignore -q [^`]+)`", raw)
    assert match
    return match.group(1).split()


def test_the_adapters_check_ignore_command_matches_a_folder_that_does_not_exist(tmp_path):
    argv = _adapter_check_ignore_argv()
    assert argv[-1] == "security-audit/"
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    assert not (tmp_path / "security-audit").exists()
    missing = subprocess.run(argv, cwd=tmp_path, env=env, check=False)
    assert missing.returncode == 1
    (tmp_path / ".gitignore").write_text("/security-audit/\n", encoding="utf-8")
    assert subprocess.run(argv, cwd=tmp_path, env=env, check=False).returncode == 0
