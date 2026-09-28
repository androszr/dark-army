"""Writes, refusals, and idempotence of the one project-root writer."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from dark_army_daemon import enrollment
from dark_army_menubar import pack_install, pack_ledger, pack_render


def _admit(monkeypatch, root: Path):
    folder = enrollment.normalise(str(root))

    def _enrolled(cwd):
        if not cwd:
            return ""
        try:
            here = enrollment.normalise(cwd)
        except (OSError, ValueError):
            here = str(cwd)
        return folder if here == folder else ""

    monkeypatch.setattr(enrollment, "root_enrolled", _enrolled)
    return folder


def test_clean_install_writes_only_rendered_keys(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    expected = pack_render.render("web", "xx", "sample-app")
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    assert owned
    written = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*") if p.is_file()
    }
    assert written == set(expected)
    for key in expected:
        assert (root / key).is_file()


def test_second_resync_writes_zero_bytes(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    pack_ledger.remember(
        folder, profile="web", prefix="xx", project="sample-app",
        settings_allow_owned=owned, last_result="ok")
    before = {
        p.relative_to(root).as_posix(): p.stat().st_mtime_ns
        for p in root.rglob("*") if p.is_file()
    }
    time.sleep(0.05)
    pack_install.resync_all()
    after = {
        p.relative_to(root).as_posix(): p.stat().st_mtime_ns
        for p in root.rglob("*") if p.is_file()
    }
    assert after == before


def test_editing_managed_region_rewrites_only_that_file(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    pack_ledger.remember(
        folder, profile="web", prefix="xx", project="sample-app",
        settings_allow_owned=owned, last_result="ok")
    target = root / "AGENTS.md"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("working agreement", "CHANGED"), encoding="utf-8")
    before = {
        p.relative_to(root).as_posix(): p.stat().st_mtime_ns
        for p in root.rglob("*") if p.is_file()
    }
    time.sleep(0.05)
    pack_install.resync_all()
    after = {
        p.relative_to(root).as_posix(): p.stat().st_mtime_ns
        for p in root.rglob("*") if p.is_file()
    }
    changed = [key for key, stamp in after.items() if stamp != before[key]]
    assert changed == ["AGENTS.md"]
    assert "CHANGED" not in target.read_text(encoding="utf-8")


def test_handwritten_context_survives_below_region(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "context.md").write_text(
        "# Architecture\nkeep this paragraph\n", encoding="utf-8")
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    text = (root / "docs" / "context.md").read_text(encoding="utf-8")
    assert pack_render.BEGIN_MARK in text
    assert text.endswith("# Architecture\nkeep this paragraph\n")


def test_dotdot_and_absolute_keys_are_refused():
    assert not pack_install._admissible("../etc/passwd")
    assert not pack_install._admissible("/etc/passwd")
    assert not pack_install._admissible("scripts/../../secret")
    assert pack_install._admissible("CLAUDE.md")
    assert pack_install._admissible(".claude/skills/ship/SKILL.md")


def test_scripts_symlink_outside_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "scripts").symlink_to(outside)
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "xx", "sample-app")
    assert ok, detail
    escaped = list(outside.rglob("*"))
    assert escaped == []
    assert pack_install._destination(folder, "scripts/check-entitlements.py") is None


def test_unenrolled_root_is_skipped_and_kept(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = enrollment.normalise(str(root))
    pack_ledger.remember(
        folder, profile="web", prefix="xx", project="sample-app",
        last_result="ok")
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: "")
    pack_install.resync_all()
    row = pack_ledger.entry(folder)
    assert row is not None
    assert row["last_result"] == "not enrolled"
    assert not (root / "CLAUDE.md").exists()


def test_host_build_sh_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "host").mkdir(parents=True)
    (root / "host" / "build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    _admit(monkeypatch, root)
    ok, detail, owned = pack_install.install_pack(
        str(root), "web", "xx", "sample-app")
    assert not ok
    assert "own project" in detail
    assert owned == []
    assert not (root / "CLAUDE.md").exists()


def test_pack_root_none_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    _admit(monkeypatch, root)
    monkeypatch.setattr(pack_install, "pack_root", lambda: None)
    ok, detail, owned = pack_install.install_pack(
        str(root), "web", "xx", "sample-app")
    assert not ok
    assert owned == []
    assert list(root.rglob("*")) == []
    pack_install.resync_all()  # must not raise


def test_executables_land_0755(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "xx", "sample-app")
    assert ok, detail
    mapping = pack_render.render("ios", "xx", "sample-app")
    for key in pack_render.executable_keys(mapping):
        mode = (root / key).stat().st_mode
        assert mode & stat.S_IXUSR, key
        assert stat.S_IMODE(mode) == 0o755, key


def test_unparseable_settings_are_left_alone(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{nope", encoding="utf-8")
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert not ok
    assert "not valid JSON" in detail
    assert settings.read_text(encoding="utf-8") == "{nope"
    assert (root / "CLAUDE.md").is_file()
    row = pack_ledger.entry(folder)
    assert row is not None
    assert "not valid JSON" in row["last_result"]


def test_forget_during_resync_is_not_undone(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    raw = dict(pack_ledger.entry(folder))
    writes = []
    real = pack_install._write_render

    def spy(*args, **kwargs):
        writes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pack_install, "_write_render", spy)
    pack_ledger.forget(folder)
    pack_install._resync_one(folder, raw, pack_install.pack_root())
    assert writes == []
    assert pack_ledger.entry(folder) is None


def test_forget_mid_write_does_not_reinsert(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    real = pack_install._write_render

    def wrapped(root_arg, mapping, **kwargs):
        pack_ledger.forget(folder)
        return real(root_arg, mapping, **kwargs)

    monkeypatch.setattr(pack_install, "_write_render", wrapped)
    pack_install.resync_all()
    assert pack_ledger.entry(folder) is None


def test_cap_refused_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    for i in range(pack_ledger.MAX_PACK_PROJECTS):
        pack_ledger.remember(
            str(tmp_path / f"p{i}"), profile="web", prefix="xx",
            project=f"p{i}")
    root = tmp_path / "overflow"
    root.mkdir()
    folder = _admit(monkeypatch, root)
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "overflow")
    assert not ok
    assert "already syncing" in detail
    assert owned == []
    assert not (root / "CLAUDE.md").exists()
    assert len(pack_ledger.published()) == pack_ledger.MAX_PACK_PROJECTS


def test_profile_switch_unlinks_generated_keeps_prose(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    security = root / ".claude" / "agents" / "xx-security-reviewer.md"
    deploy = root / ".claude" / "skills" / "deploy-web" / "SKILL.md"
    assert security.is_file()
    assert deploy.is_file()
    claude = root / "CLAUDE.md"
    claude.write_text(claude.read_text(encoding="utf-8") + "keep me\n",
                      encoding="utf-8")
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "xx", "sample-app")
    assert ok, detail
    # Canonical .claude trees are left alone; only generated mirrors prune.
    # Every profile ships a security reviewer now: the switch swaps the web
    # brief for the generic one rather than removing it.
    assert security.is_file()
    assert "NEXT_PUBLIC" not in security.read_text(encoding="utf-8")
    assert deploy.is_file()
    assert (root / ".codex" / "agents" / "xx-security-reviewer.toml").is_file()
    assert not (root / ".agents" / "skills" / "deploy-web" / "SKILL.md").exists()
    assert (root / ".codex" / "agents" / "xx-app-reviewer.toml").is_file()
    assert (root / ".claude" / "agents" / "xx-app-reviewer.md").is_file()
    assert claude.read_text(encoding="utf-8").endswith("keep me\n")
    assert (root / "docs" / "context.md").is_file()


def test_hand_added_workflow_and_gitnexus_skill_survive_resync(
        tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    workflow = root / ".github" / "workflows" / "lint.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("name: lint\n", encoding="utf-8")
    extra_agent = root / ".claude" / "agents" / "mine.md"
    extra_agent.write_text("# mine\n", encoding="utf-8")
    gn_claude = root / ".claude" / "skills" / "gitnexus-exploring" / "SKILL.md"
    gn_claude.parent.mkdir(parents=True)
    gn_claude.write_text("# gitnexus\n", encoding="utf-8")
    gn_agents = root / ".agents" / "skills" / "gitnexus-exploring" / "SKILL.md"
    gn_agents.parent.mkdir(parents=True)
    gn_agents.write_text("# gitnexus\n", encoding="utf-8")
    pack_install.resync_all()
    assert workflow.read_text(encoding="utf-8") == "name: lint\n"
    assert extra_agent.read_text(encoding="utf-8") == "# mine\n"
    assert gn_claude.is_file()
    assert gn_agents.is_file()


def test_settings_merge_failure_does_not_unlink(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    stray = root / ".codex" / "agents" / "stray.toml"
    stray.write_text("name = \"stray\"\n", encoding="utf-8")
    settings = root / ".claude" / "settings.json"
    settings.write_text("{nope", encoding="utf-8")
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "xx", "sample-app")
    assert not ok
    assert "not valid JSON" in detail
    assert stray.is_file()


def test_self_roots_sees_host_build_sh_without_repo_stamp(tmp_path, monkeypatch):
    from dark_army_menubar import dev_build
    root = tmp_path / "proj"
    (root / "host").mkdir(parents=True)
    (root / "host" / "build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    folder = enrollment.normalise(str(root))
    assert pack_install._is_bobs_own(folder)
    assert folder in pack_install.self_roots([str(root)])


def test_resync_skips_a_root_being_written(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    writes = []
    real = pack_install._write_render

    def spy(*args, **kwargs):
        writes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pack_install, "_write_render", spy)
    pack_install._begin_write(folder, wait=True)
    try:
        pack_install.resync_all()
        assert writes == []
    finally:
        pack_install._end_write(folder)


def test_staleness_walk_sees_dot_directories(tmp_path):
    from dark_army_menubar import dev_build
    skill = (
        tmp_path / "host" / "dark_army_menubar" / "agent_pack"
        / "template" / ".claude" / "skills" / "ship" / "SKILL.md"
    )
    skill.parent.mkdir(parents=True)
    skill.write_text("x\n", encoding="utf-8")
    os.utime(skill, (2_000_000_000, 2_000_000_000))
    newest = dev_build._newest_mtime(tmp_path, [
        ("host/dark_army_menubar/agent_pack", ("**/*",)),
    ])
    assert newest >= 2_000_000_000


def test_install_writes_the_model_line_and_a_resync_rewrites_it(
        tmp_path, monkeypatch):
    """The plumbing end to end: `install_pack(models=)` lands the line,
    `resync_all(models_for=)` with a changed table rewrites it
    (`_already_current` sees different bytes), and a table of Defaults
    takes it out again."""
    from dark_army_daemon import agent_models
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    table = agent_models.resolve({}, folder)
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app", models=table)
    assert ok, detail
    brief = root / ".claude/agents/xx-planner.md"
    assert "\nmodel: opus\n" in brief.read_text(encoding="utf-8")
    assert 'model = "gpt-6-astra"' in (
        root / ".codex/agents/xx-planner.toml").read_text(encoding="utf-8")
    assert "model: grok-4.6" in (
        root / ".grok/agents/xx-planner.md").read_text(encoding="utf-8")
    for role in ("planner", "implementer", "verifier", "bug-auditor",
                 "integration-reviewer", "security-reviewer", "card-preparer"):
        shim = (root / f".codex/agents/xx-{role}.toml").read_text(encoding="utf-8")
        assert f'model = "{agent_models.SHIPPED["codex"][role]}"' in shim
    pack_ledger.remember(
        folder, profile="web", prefix="xx", project="sample-app",
        settings_allow_owned=owned, last_result="ok")

    changed = agent_models.resolve(
        {"agent_models": {"claude": {"planner": "sonnet"}}}, folder)
    asked = []

    def models_for(r):
        asked.append(r)
        return changed

    pack_install.resync_all(models_for=models_for)
    assert asked == [folder]
    assert "\nmodel: sonnet\n" in brief.read_text(encoding="utf-8")
    assert "model: opus" not in brief.read_text(encoding="utf-8")

    pack_install.resync_all(models_for=lambda r: None)
    assert "model:" not in brief.read_text(encoding="utf-8").split("---")[1]


def test_delivery_leads_install_inside_admitted_project(tmp_path, monkeypatch):
    from dark_army_daemon import areas
    root = tmp_path / "project"; root.mkdir()
    monkeypatch.setattr("dark_army_daemon.paths.AGENT_PACK_PATH", tmp_path / "ledger.json")
    folder = _admit(monkeypatch, root)
    expected = pack_render.render("web", "xx", "test-project")
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "test-project")
    assert ok, detail
    for area in areas.AREAS:
        key = areas.brief_path(area.slug)
        if key in expected:
            assert (root / key).read_bytes() == expected[key]
        else:
            assert not (root / key).exists()
    assert not pack_install._admissible(".claude/leads/../../outside.md")


def test_the_shunt_skill_lands_0644_and_workers_json_follows_the_worker_cell(
        tmp_path, monkeypatch):
    from dark_army_daemon import agent_models
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr("dark_army_daemon.paths.AGENT_PACK_PATH",
                        tmp_path / "agent-pack.json")
    folder = _admit(monkeypatch, root)
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app", models=agent_models.resolve({}, folder))
    assert ok, detail
    for name in ("SKILL.md", "bulk_read.py", "code_write.py", "exempt.py", "workers.json"):
        for tree in (".claude", ".agents"):
            landed = root / tree / "skills" / "shunt" / name
            assert landed.is_file(), landed
            assert stat.S_IMODE(landed.stat().st_mode) == 0o644, landed
    assert (root / ".agents/skills/shunt/agents/openai.yaml").is_file()
    workers = root / ".claude/skills/shunt/workers.json"
    assert workers.read_text() == '{"claude": "haiku", "codex": "gpt-6-luna", "grok": "grok-4.5"}\n'
    for row in ("Bash(python3 .claude/skills/shunt/bulk_read.py:*)",
                "Bash(python3 .claude/skills/shunt/code_write.py:*)",
                "Bash(python3 .claude/skills/shunt/exempt.py:*)"):
        assert row in owned
    pack_ledger.remember(folder, profile="web", prefix="xx", project="sample-app",
                         settings_allow_owned=owned, last_result="ok")

    changed = agent_models.resolve({"agent_models": {"grok": {"worker": "grok-4.6"}}}, folder)
    pack_install.resync_all(models_for=lambda r: changed)
    assert workers.read_text() == '{"claude": "haiku", "codex": "gpt-6-luna", "grok": "grok-4.6"}\n'
    assert (root / ".agents/skills/shunt/workers.json").read_bytes() == workers.read_bytes()


def test_a_projects_env_key_survives_two_installs_and_the_rows_are_replaced(
        tmp_path, monkeypatch):
    """The pack writes no `env` and no `hooks`: a project's own threshold
    override is a foreign key `merge_settings` keeps, and the owned allow rows
    are replaced rather than duplicated."""
    import json
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"BOB_SHUNT_MIN_LINES": "600"},
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "/theirs"}]}]},
        "permissions": {"allow": ["Bash(make:*)"]},
    }))
    monkeypatch.setattr("dark_army_daemon.paths.AGENT_PACK_PATH",
                        tmp_path / "agent-pack.json")
    folder = _admit(monkeypatch, root)
    for _ in range(2):
        ok, detail, owned = pack_install.install_pack(folder, "web", "xx", "sample-app")
        assert ok, detail
        pack_ledger.remember(folder, profile="web", prefix="xx", project="sample-app",
                             settings_allow_owned=owned, last_result="ok")
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    assert settings["env"] == {"BOB_SHUNT_MIN_LINES": "600"}
    assert settings["hooks"] == {"PreToolUse": [{"hooks": [{"type": "command", "command": "/theirs"}]}]}
    allow = settings["permissions"]["allow"]
    assert "Bash(make:*)" in allow
    assert allow.count("Bash(python3 .claude/skills/shunt/exempt.py:*)") == 1


def test_a_resync_adds_the_guard_rows_once_and_keeps_the_projects_deny(
        tmp_path, monkeypatch):
    """The pack's deny rows reach a project whose settings.json already
    exists, once, beside the project's own deny rows, and the ledger records
    them as owned (`settings_deny_owned`)."""
    import json
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": [], "deny": ["Bash(git push:*)"]},
    }))
    monkeypatch.setattr("dark_army_daemon.paths.AGENT_PACK_PATH",
                        tmp_path / "agent-pack.json")
    folder = _admit(monkeypatch, root)
    for _ in range(2):
        ok, detail, _owned = pack_install.install_pack(
            folder, "web", "xx", "sample-app")
        assert ok, detail
    deny = json.loads(
        (root / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert deny[0] == "Bash(git push:*)"
    assert deny.count("Read(**/.dark-army/key)") == 1
    assert deny.count("Read(~/.dark-army/api-token)") == 1
    assert "Read(**/.dark-army/key)" in pack_ledger.entry(folder)["settings_deny_owned"]


def test_detect_app_reads_the_xcodeproj_name(tmp_path):
    (tmp_path / "ios" / "Ledgerly.xcodeproj").mkdir(parents=True)
    assert pack_install.detect_app(str(tmp_path)) == "Ledgerly"


def test_detect_app_is_empty_without_exactly_one_project(tmp_path):
    assert pack_install.detect_app(str(tmp_path)) == ""
    (tmp_path / "ios" / "A.xcodeproj").mkdir(parents=True)
    (tmp_path / "ios" / "B.xcodeproj").mkdir()
    assert pack_install.detect_app(str(tmp_path)) == ""


def test_install_names_the_app_after_the_xcodeproj_not_the_folder(
        tmp_path, monkeypatch):
    root = tmp_path / "finance-demo"
    (root / "ios" / "Ledgerly.xcodeproj").mkdir(parents=True)
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "sf", "finance-demo")
    assert ok, detail
    text = (root / ".github" / "workflows" / "testflight.yml").read_text()
    assert "ios/Ledgerly.xcodeproj" in text
    assert "FinanceDemo" not in text
