"""The Agent models setting reaches Dark Army's own checkout.

The pack never renders into its own project, so before 22 Sep 2026 the
`bc-*` briefs there carried no `model:` line and every role — planner,
implementer, verifier, reviewers, on Claude, Codex and Grok — ran on the
parent session's model: the day's transcripts showed `bc-planner` on
`claude-opus-5` with the planner set to `claude-fable-5-1`, and even the
Sonnet verifier on Opus. `pack_install.pin_own_checkout` pins the one model
line in place; a model press re-syncs at once rather than at next launch.
"""

import threading
from pathlib import Path

from dark_army_daemon import agent_models
from dark_army_menubar import dev_build, pack_install, pack_render

WORKERS_KEYS = (".claude/skills/shunt/workers.json",
                ".agents/skills/shunt/workers.json")
ROLES = ("planner", "implementer", "verifier", "bug-auditor",
         "integration-reviewer", "security-reviewer", "card-preparer")


def _own_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "dark-army"
    (root / "host").mkdir(parents=True)
    (root / "host" / "build.sh").write_text("#!/bin/sh\n")  # the self marker
    for role in ROLES:
        name = f"bc-{role}"
        brief = root / ".claude" / "agents" / f"{name}.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(f"---\nname: {name}\ndescription: d\ntools: Read\n---\n\nBody.\n")
        fm = {"name": name, "description": "d", "tools": "Read"}
        codex = root / ".codex" / "agents" / f"{name}.toml"
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.write_text(pack_render.codex_shim(name, fm, "read-only"))
        grok = root / ".grok" / "agents" / f"{name}.md"
        grok.parent.mkdir(parents=True, exist_ok=True)
        grok.write_text(pack_render.grok_shim(name, fm, "read-only"))
    for key in WORKERS_KEYS:
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pack_render.workers_json(None).decode())
    hand = root / ".claude" / "agents" / "mission-control.md"
    hand.write_text("---\nname: mission-control\nmodel: sonnet\n---\n\nBody.\n")
    return root


def _settings():
    return {"agent_models": {
        "claude": {"planner": "claude-fable-5-1", "implementer": "claude-opus-5"},
        "codex": {"implementer": "gpt-5.6-sol"},
        "grok": {"planner": "grok-4.7", "verifier": "grok-4.6", "worker": "grok-4.5"},
    }}


def _model_line(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("model:") or line.startswith("model ="):
            return line
    return ""


def test_every_role_on_every_provider_gets_its_model(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    root = _own_checkout(tmp_path)
    table = agent_models.resolve(_settings(), str(root))
    wrote = pack_install.pin_own_checkout(str(root), table)
    assert wrote
    for role in ROLES:
        name = f"bc-{role}"
        for provider, path, spell in (
                ("claude", root / ".claude/agents" / f"{name}.md", "model: {}"),
                ("codex", root / ".codex/agents" / f"{name}.toml", 'model = "{}"'),
                ("grok", root / ".grok/agents" / f"{name}.md", "model: {}")):
            want = table[provider][role]
            got = _model_line(path)
            assert got == (spell.format(want) if want else ""), (provider, role, got)
    assert _model_line(root / ".claude/agents/bc-planner.md") == "model: claude-fable-5-1"
    assert _model_line(root / ".codex/agents/bc-implementer.toml") == 'model = "gpt-5.6-sol"'
    assert _model_line(root / ".grok/agents/bc-planner.md") == "model: grok-4.7"
    # The reasoning line and the body are untouched.
    codex = (root / ".codex/agents/bc-planner.toml").read_text()
    assert 'model_reasoning_effort = "high"' in codex
    assert (root / ".claude/agents/bc-planner.md").read_text().endswith("\n\nBody.\n")
    # The shunt twins stay the template's bytes (sync_shunt_skill --check),
    # and a hand-named brief keeps its own line.
    for key in WORKERS_KEYS:
        assert (root / key).read_bytes() == pack_render.workers_json(None)
    assert _model_line(root / ".claude/agents/mission-control.md") == "model: sonnet"


def test_repo_codex_shims_match_the_shipped_role_policy():
    root = Path(__file__).resolve().parents[2]
    for role in ROLES:
        path = root / ".codex" / "agents" / f"bc-{role}.toml"
        assert _model_line(path) == f'model = "{agent_models.SHIPPED["codex"][role]}"'
        assert 'model_reasoning_effort = "high"' in path.read_text()


def test_a_second_pass_changes_nothing_and_default_removes_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    root = _own_checkout(tmp_path)
    table = agent_models.resolve(_settings(), str(root))
    pack_install.pin_own_checkout(str(root), table)
    assert pack_install.pin_own_checkout(str(root), table) == []
    table["claude"]["planner"] = ""  # Default: no flag, no stale pin
    assert pack_install.pin_own_checkout(str(root), table) == [".claude/agents/bc-planner.md"]
    assert _model_line(root / ".claude/agents/bc-planner.md") == ""


def test_a_project_that_is_not_dark_army_is_never_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    root = _own_checkout(tmp_path)
    (root / "host" / "build.sh").unlink()
    before = (root / ".claude/agents/bc-planner.md").read_bytes()
    assert pack_install.pin_own_checkout(str(root), agent_models.resolve(_settings(), str(root))) == []
    assert (root / ".claude/agents/bc-planner.md").read_bytes() == before


def test_a_linked_brief_is_not_followed(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    root = _own_checkout(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: bc-planner\n---\n")
    brief = root / ".claude/agents/bc-planner.md"
    brief.unlink()
    brief.symlink_to(outside)
    pack_install.pin_own_checkout(str(root), agent_models.resolve(_settings(), str(root)))
    assert outside.read_text() == "---\nname: bc-planner\n---\n"


def test_the_resync_pins_the_own_checkout_even_with_no_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    root = _own_checkout(tmp_path)
    monkeypatch.setattr(pack_install, "self_roots", lambda: [str(root)])
    monkeypatch.setattr(pack_install, "pack_root", lambda: None)
    pack_install.resync_all(models_for=lambda r: agent_models.resolve(_settings(), r))
    assert _model_line(root / ".claude/agents/bc-verifier.md") == "model: " + \
        agent_models.SHIPPED["claude"]["verifier"]


def test_a_resync_asked_for_mid_pass_runs_once_more(monkeypatch):
    """A model press while a pass runs is not dropped: the pass goes round
    again, so the last choice is always the one on disk."""
    passes = []
    started, release = threading.Event(), threading.Event()

    def fake(models_for):
        passes.append(1)
        if len(passes) == 1:
            started.set()
            release.wait(5)

    monkeypatch.setattr(pack_install, "_resync_all_locked", fake)
    runner = threading.Thread(target=pack_install.resync_all)
    runner.start()
    assert started.wait(5)
    pack_install.resync_all()  # arrives mid-pass
    release.set()
    runner.join(5)
    assert len(passes) == 2
    pack_install.resync_all()
    assert len(passes) == 3


def test_a_model_press_resyncs_at_once():
    src = Path(__file__).resolve().parents[1] / "dark_army_menubar" / "app.py"
    body = src.read_text().split("def _set_agent_model(self, value)")[1].split("\n    def ")[0]
    assert body.count("self._resync_agent_packs()") == 2
