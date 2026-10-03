"""Pure render of the vendored agent pack. Writes nothing."""

from __future__ import annotations

import json
import re

import pytest
from pathlib import Path

from dark_army_daemon import card_preparer_brief
from dark_army_menubar import pack_install, pack_render

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def _mapping(profile="web", prefix="xx", project="sample-app", **kwargs):
    return pack_render.render(profile, prefix, project, **kwargs)


def test_unknown_profile_raises():
    with pytest.raises(pack_render.PackRenderError, match="unknown profile"):
        pack_render.render("android", "xx", "sample-app")


def test_bad_prefix_raises():
    with pytest.raises(pack_render.PackRenderError, match="prefix"):
        pack_render.render("web", "NOPE", "sample-app")


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_render_keys_are_admissible_and_relative(profile):
    mapping = _mapping(profile)
    assert mapping
    for key in mapping:
        assert not key.startswith("/"), key
        assert ".." not in key.split("/"), key
        assert pack_install._admissible(key), key


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_no_unresolved_placeholders(profile):
    mapping = _mapping(profile)
    leftovers = []
    for key, data in mapping.items():
        leftovers.extend(f"{key}" for _ in _PLACEHOLDER.finditer(key))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        leftovers.extend(
            f"{key}: {{{{{m.group(1)}}}}}" for m in _PLACEHOLDER.finditer(text)
        )
    assert leftovers == []


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_no_home_paths(profile):
    mapping = _mapping(profile)
    for key, data in mapping.items():
        blob = data if isinstance(data, bytes) else data.encode()
        assert b"/Users/" not in blob, key
        assert b"/home/" not in blob, key
        assert "/Users/" not in key
        assert "/home/" not in key


def test_prefix_reaches_keys_and_values():
    mapping = _mapping("web", prefix="xx")
    assert ".claude/agents/xx-planner.md" in mapping
    brief = mapping[".claude/agents/xx-planner.md"].decode()
    assert "name: xx-planner" in brief
    assert "{{P}}" not in brief


def test_fragment_placeholder_resolves_on_second_pass():
    mapping = _mapping("web", prefix="zz", project="sample-app")
    context = mapping["docs/context.md"].decode()
    assert "zz-security-reviewer" in context
    assert "{{P}}" not in context
    assert "{{REVIEWER_AGENT}}" not in context


def test_both_questions_carry_both_branches():
    mapping = _mapping("both")
    questions = mapping[".claude/skills/ship/templates/questions.md"].decode()
    assert "### web-next-vercel" in questions
    assert "### ios-swift-testflight" in questions


def test_web_questions_have_no_profile_headings():
    mapping = _mapping("web")
    questions = mapping[".claude/skills/ship/templates/questions.md"].decode()
    assert "### web-next-vercel" not in questions


def test_skill_mirror_is_byte_identical_plus_openai_yaml():
    mapping = _mapping("web")
    claude = {
        key[len(".claude/skills/"):]: data
        for key, data in mapping.items()
        if key.startswith(".claude/skills/")
    }
    agents = {
        key[len(".agents/skills/"):]: data
        for key, data in mapping.items()
        if key.startswith(".agents/skills/")
    }
    skill_names = {key.split("/", 1)[0] for key in claude}
    for name in skill_names:
        assert not name.startswith("gitnexus-")
        yaml_key = f"{name}/{pack_render.OPENAI_YAML}"
        assert yaml_key in agents
        for inner, data in claude.items():
            if inner.startswith(name + "/"):
                assert agents[inner] == data
    extra = set(agents) - set(claude)
    assert extra == {f"{name}/{pack_render.OPENAI_YAML}" for name in skill_names}


def test_gitnexus_skills_are_not_mirrored():
    mapping = {
        ".claude/skills/gitnexus-exploring/SKILL.md": b"---\nname: g\n---\n",
        ".claude/skills/review/SKILL.md": b"---\nname: review\ndescription: d.\n---\n",
    }
    pack_render.mirror_skills(mapping)
    assert ".agents/skills/review/SKILL.md" in mapping
    assert ".agents/skills/review/agents/openai.yaml" in mapping
    assert not any(
        key.startswith(".agents/skills/gitnexus-") for key in mapping
    )


def test_one_shim_pair_per_brief():
    mapping = _mapping("both")
    briefs = [
        key for key in mapping
        if key.startswith(".claude/agents/") and key.endswith(".md")
    ]
    assert briefs
    for brief in briefs:
        name = brief.rsplit("/", 1)[-1][:-3]
        assert f".codex/agents/{name}.toml" in mapping
        assert f".grok/agents/{name}.md" in mapping


def test_sandbox_modes():
    mapping = _mapping("both", prefix="xx")
    readonly = {
        "xx-verifier",
        "xx-bug-auditor",
        "xx-security-reviewer",
        "xx-app-reviewer",
    }
    writable = {"xx-planner", "xx-implementer"}
    for name in readonly:
        toml = mapping[f".codex/agents/{name}.toml"].decode()
        assert 'sandbox_mode = "read-only"' in toml, name
    for name in writable:
        toml = mapping[f".codex/agents/{name}.toml"].decode()
        assert 'sandbox_mode = "workspace-write"' in toml, name


def test_both_renders_at_least_sixty_files():
    mapping = _mapping("both")
    assert len(mapping) >= 60


def test_pack_carries_the_card_preparer():
    mapping = _mapping("web", prefix="xx")
    key = ".claude/agents/xx-card-preparer.md"
    assert key in mapping
    text = mapping[key].decode()
    body = text.split("---", 2)[2].lstrip("\n")
    assert body == card_preparer_brief.BRIEF
    assert "name: xx-card-preparer" in text
    assert ".codex/agents/xx-card-preparer.toml" in mapping
    assert ".grok/agents/xx-card-preparer.md" in mapping


def test_web_and_ios_are_smaller_than_both():
    both = len(_mapping("both"))
    assert len(_mapping("web")) < both
    assert len(_mapping("ios")) < both


def test_executable_keys_are_scripts_and_shell():
    mapping = _mapping("ios")
    keys = pack_render.executable_keys(mapping)
    assert any(k.endswith("preflight.sh") for k in keys)
    assert any(k.endswith("close-out.sh") for k in keys)
    assert ".claude/skills/ship/gate.sh" in keys and ".agents/skills/ship/gate.sh" in keys
    # The scout checker runs as `python3 <path>`, so it is not 0755.
    assert ".claude/skills/scout/scout_check.py" not in keys
    assert ".agents/skills/scout/scout_check.py" not in keys
    assert any(k.startswith("scripts/") and k.endswith(".py") for k in keys)
    assert all(
        k.endswith(".sh") or (k.startswith("scripts/") and k.endswith(".py"))
        for k in keys
    )


def test_splice_keeps_bytes_outside_markers():
    managed = b"NEW\n"
    existing = (
        b"HEAD\n"
        + pack_render.BEGIN_MARK.encode() + b"\nOLD\n"
        + pack_render.END_MARK.encode() + b"\nTAIL\n"
    )
    out = pack_render.splice(existing, managed)
    assert out.startswith(b"HEAD\n")
    assert out.endswith(b"TAIL\n")
    assert b"NEW\n" in out
    assert b"OLD\n" not in out


def test_splice_unmarked_prepends_and_keeps_old():
    old = b"# handwritten notes\nkeep me\n"
    out = pack_render.splice(old, b"MANAGED\n")
    assert pack_render.BEGIN_MARK.encode() in out
    assert out.endswith(b"# handwritten notes\nkeep me\n")
    assert b"MANAGED\n" in out


def test_splice_empty_is_region_alone():
    out = pack_render.splice(b"", b"MANAGED\n")
    assert out.startswith((pack_render.BEGIN_MARK + "\n").encode())
    assert out.endswith((pack_render.END_MARK + "\n").encode())
    assert b"MANAGED\n" in out


def test_splice_twice_is_idempotent():
    first = pack_render.splice(b"notes\n", b"MANAGED\n")
    second = pack_render.splice(first, b"MANAGED\n")
    assert first == second


def test_merge_settings_keeps_foreign_rows_and_keys():
    existing = json.dumps({
        "permissions": {
            "allow": ["Bash(git status:*)", "Bash(mine:*)"],
            "deny": ["Bash(git push:*)"],
        },
        "extra": True,
    }).encode()
    managed = json.dumps({
        "permissions": {
            "allow": ["Bash(git status:*)", "Bash(pnpm lint)"],
        },
    }).encode()
    out, owned, owned_deny = pack_render.merge_settings(
        existing, managed, owned=["Bash(git status:*)"])
    parsed = json.loads(out)
    assert parsed["extra"] is True
    assert parsed["permissions"]["deny"] == ["Bash(git push:*)"]
    assert "Bash(mine:*)" in parsed["permissions"]["allow"]
    assert "Bash(pnpm lint)" in parsed["permissions"]["allow"]
    assert "Bash(git status:*)" in parsed["permissions"]["allow"]
    assert owned == ["Bash(git status:*)", "Bash(pnpm lint)"]
    assert owned_deny == []


def test_merge_settings_owns_its_deny_rows_and_keeps_the_projects():
    """A project's own deny rows survive; the pack's are replaced as a set,
    and an older ledger row (owned_deny None) removes nothing."""
    existing = json.dumps({
        "permissions": {
            "allow": [],
            "deny": ["Bash(git push:*)", "Read(~/.dark-army/old)"],
        },
    }).encode()
    managed = json.dumps({
        "permissions": {
            "allow": [],
            "deny": ["Read(**/.dark-army/key)"],
        },
    }).encode()
    out, _allow, owned_deny = pack_render.merge_settings(
        existing, managed, [], owned_deny=["Read(~/.dark-army/old)"])
    deny = json.loads(out)["permissions"]["deny"]
    assert deny == ["Bash(git push:*)", "Read(**/.dark-army/key)"]
    assert owned_deny == ["Read(**/.dark-army/key)"]
    out, _allow, _owned = pack_render.merge_settings(existing, managed, [])
    deny = json.loads(out)["permissions"]["deny"]
    assert deny == ["Bash(git push:*)", "Read(~/.dark-army/old)",
                    "Read(**/.dark-army/key)"]


def test_the_template_denies_reading_dark_armys_secrets():
    """The guard rows are a second layer beside the desk/session split
    (docs/agent-pack.md): the enrolment key, the session-token file and the
    phone/relay/bot keys, never the whole state folder — agents are told to
    read ~/.dark-army/search-scope.json."""
    rows = pack_render.settings_deny_rows(
        _mapping("web")[pack_render.SETTINGS_KEY])
    for row in ("Read(~/.dark-army/api-token)", "Read(**/.dark-army/key)",
                "Read(~/.bob-companion/**)",
                "Bash(cat ~/.dark-army/grok-bot*)"):
        assert row in rows
    assert not any("search-scope" in row or row == "Read(~/.dark-army/**)"
                   for row in rows)


def test_merge_settings_refuses_unparseable():
    with pytest.raises(pack_render.PackRenderError, match="not valid JSON"):
        pack_render.merge_settings(b"{nope", b'{"permissions":{}}', [])


def test_settings_json_parses_after_render():
    mapping = _mapping("both")
    parsed = json.loads(mapping[pack_render.SETTINGS_KEY])
    assert isinstance(parsed["permissions"]["allow"], list)
    assert any("pnpm lint" in row for row in parsed["permissions"]["allow"])


def test_default_prefix_from_hyphenated_name():
    assert pack_render.default_prefix("sample-app") == "sa"
    assert pack_render.default_prefix("123") == "p123"
    assert pack_render.default_prefix("") == "app"


# --- The per-role model table, written into each brief and its two shims ---

def _model_lines(data: bytes) -> list[str]:
    return re.findall(r"^model[ :=].*$", data.decode("utf-8"), re.M)


def _shipped():
    from dark_army_daemon import agent_models
    return agent_models.resolve({}, "/tmp/sample")


def test_render_with_the_shipped_model_table_pins_every_slot():
    from dark_army_daemon import agent_models
    mapping = _mapping("both", models=_shipped())
    claude = mapping[".claude/agents/xx-planner.md"].decode("utf-8")
    fm = pack_render._frontmatter(claude)
    assert fm["model"] == "opus"
    assert fm["name"] == "xx-planner"
    assert _model_lines(mapping[".codex/agents/xx-planner.toml"])[0] == (
        'model = "gpt-6-astra"')
    assert _model_lines(mapping[".grok/agents/xx-planner.md"]) == [
        "model: grok-4.6"]
    # Each Codex checker rides its role's GPT-6 tier.
    assert pack_render._frontmatter(
        mapping[".claude/agents/xx-security-reviewer.md"].decode("utf-8")
    )["model"] == "sonnet"
    assert _model_lines(mapping[".codex/agents/xx-security-reviewer.toml"])[0] == (
        'model = "gpt-6-astra"')
    assert _model_lines(mapping[".grok/agents/xx-security-reviewer.md"]) == [
        "model: grok-4.5"]
    for role in ("planner", "implementer", "verifier", "bug-auditor",
                 "integration-reviewer", "security-reviewer", "card-preparer"):
        assert _model_lines(mapping[f".codex/agents/xx-{role}.toml"])[0] == (
            f'model = "{agent_models.SHIPPED["codex"][role]}"')


def test_the_model_line_sits_where_each_cli_reads_it():
    """Claude: after `name:` inside the frontmatter. Codex: after
    `description`. Grok: after `description:` inside the frontmatter."""
    mapping = _mapping("web", models=_shipped())
    claude = mapping[".claude/agents/xx-verifier.md"].decode("utf-8").split("\n")
    assert claude[0] == "---"
    assert claude[1] == "name: xx-verifier"
    assert claude[2] == "model: sonnet"
    codex = mapping[".codex/agents/xx-verifier.toml"].decode("utf-8").split("\n")
    at = next(i for i, line in enumerate(codex) if line.startswith("description = "))
    assert codex[at + 1] == 'model = "gpt-6-luna"'
    grok = mapping[".grok/agents/xx-verifier.md"].decode("utf-8").split("\n")
    at = next(i for i, line in enumerate(grok) if line.startswith("description: "))
    assert grok[at + 1] == "model: grok-4.5"
    assert grok[at + 2] == "---"


def test_a_role_that_is_not_a_slot_gets_no_model_line():
    mapping = _mapping("ios", models=_shipped())
    for key in (".claude/agents/xx-app-reviewer.md",
                ".codex/agents/xx-app-reviewer.toml",
                ".grok/agents/xx-app-reviewer.md"):
        assert _model_lines(mapping[key]) == [], key


def test_a_default_slot_writes_no_model_line_in_any_of_the_three_files():
    table = _shipped()
    table["claude"]["planner"] = ""
    table["codex"]["planner"] = ""
    table["grok"]["planner"] = ""
    mapping = _mapping("web", models=table)
    for key in (".claude/agents/xx-planner.md",
                ".codex/agents/xx-planner.toml",
                ".grok/agents/xx-planner.md"):
        assert _model_lines(mapping[key]) == [], key
    # Its neighbour still carries its own.
    assert _model_lines(mapping[".grok/agents/xx-verifier.md"]) == ["model: grok-4.5"]


def test_render_with_no_table_is_byte_identical_to_before():
    assert _mapping("both") == _mapping("both", models=None)
    empty = {p: {s: "" for s in _shipped()[p]} for p in _shipped()}
    assert _mapping("both") == _mapping("both", models=empty)
    for key, data in _mapping("both").items():
        if "/agents/" in key:
            assert _model_lines(data) == [], key


def test_the_shims_ride_a_different_model_per_provider():
    """Grok also discovers `.claude/agents`, so the Grok shim must carry a
    Grok name, never the Claude alias beside it."""
    mapping = _mapping("web", models=_shipped())
    claude = pack_render._frontmatter(
        mapping[".claude/agents/xx-planner.md"].decode("utf-8"))["model"]
    grok = pack_render._frontmatter(
        mapping[".grok/agents/xx-planner.md"].decode("utf-8"))["model"]
    assert claude == "opus" and grok == "grok-4.6"


def test_pin_model_replaces_rather_than_adding_a_second_line():
    text = "---\nname: xx-planner\nmodel: haiku\ndescription: d\n---\n\nbody\n"
    pinned = pack_render.pin_model(text, "opus")
    assert pinned.count("model:") == 1
    assert pinned == "---\nname: xx-planner\nmodel: opus\ndescription: d\n---\n\nbody\n"
    assert pack_render.pin_model(pinned, "") == (
        "---\nname: xx-planner\ndescription: d\n---\n\nbody\n")
    assert pack_render.pin_model("no frontmatter\n", "opus") == "no frontmatter\n"
    # No `name:` line: the model leads the block.
    assert pack_render.pin_model("---\ndescription: d\n---\nb", "opus") == (
        "---\nmodel: opus\ndescription: d\n---\nb")


def test_role_of_strips_the_projects_prefix():
    assert pack_render.role_of(".claude/agents/xx-bug-auditor.md", "xx") == "bug-auditor"
    assert pack_render.role_of("{{P}}-planner.md", "{{P}}") == "planner"
    assert pack_render.role_of(".claude/agents/ab-planner.md", "") == "planner"
    assert pack_render.role_of(".claude/agents/other.md", "xx") == "other"


def test_shipped_roles_are_the_vendored_briefs_minus_the_prefix():
    roles = pack_render.shipped_roles()
    assert roles == frozenset({
        "planner", "implementer", "verifier", "bug-auditor", "card-preparer",
        "integration-reviewer", "security-reviewer", "app-reviewer",
    })


def test_a_pack_rendered_with_another_prefix_still_finds_the_role():
    mapping = pack_render.render("web", "ab", "sample-app", models=_shipped())
    assert pack_render._frontmatter(
        mapping[".claude/agents/ab-planner.md"].decode("utf-8"))["model"] == "opus"
    assert _model_lines(mapping[".codex/agents/ab-planner.toml"])[0] == (
        'model = "gpt-6-astra"')


_PROFILE_AREAS = {
    "web": {"backbone", "gate", "ledger", "universal"},
    "ios": {"pocket", "backbone", "gate", "universal"},
    "both": {"pocket", "backbone", "gate", "ledger", "universal"},
}


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_delivery_leads_render_without_prefix_rewriting(profile):
    from dark_army_daemon import areas
    mapping = _mapping(profile, prefix="xx")
    shipped = {Path(p).stem for p in mapping if p.startswith(".claude/leads/")}
    assert shipped == _PROFILE_AREAS[profile]
    for area in areas.AREAS:
        path = areas.brief_path(area.slug)
        if area.slug not in shipped:
            continue
        assert mapping[path].startswith(f"# {area.name}".encode())
        assert pack_install._admissible(path)


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_rendered_pack_carries_the_bounded_loop(profile):
    """Every rendered project gets the counted fix loop, on all three providers.

    The rules are `test_agent_briefs.BOUNDED_LOOP_RULES` — one tuple, so the
    local brief and the pack cannot drift apart on which phrases count — and the
    Codex and Grok shims must still point at the brief that carries them.
    """
    from tests.test_agent_briefs import BOUNDED_LOOP_BANNED, BOUNDED_LOOP_RULES

    mapping = _mapping(profile)
    brief = " ".join(mapping[".claude/agents/xx-implementer.md"].decode().split())
    for rule in BOUNDED_LOOP_RULES:
        assert rule in brief, f"{profile}: rendered brief lacks {rule!r}"
    for banned in BOUNDED_LOOP_BANNED:
        assert banned not in brief, f"{profile}: rendered brief still says {banned!r}"
    for shim in (".codex/agents/xx-implementer.toml", ".grok/agents/xx-implementer.md"):
        assert b".claude/agents/xx-implementer.md" in mapping[shim], f"{profile}: {shim} does not name the brief"


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_rendered_pack_marks_scope(profile):
    """Every rendered project's checkers mark each finding in or out of the
    plan's scope: the auditor carries every `SCOPE_MARK_RULES` phrase, the
    profile reviewers the column, and the generated shims still name the brief."""
    from tests.test_agent_briefs import SCOPE_MARK_RULES

    mapping = _mapping(profile)
    auditor = " ".join(mapping[".claude/agents/xx-bug-auditor.md"].decode().split())
    for rule in SCOPE_MARK_RULES:
        assert rule in auditor, f"{profile}: rendered auditor lacks {rule!r}"
    if profile in ("web", "both"):
        assert "| In scope |" in mapping[".claude/agents/xx-security-reviewer.md"].decode()
    if profile in ("ios", "both"):
        assert "| In scope |" in mapping[".claude/agents/xx-app-reviewer.md"].decode()
    for shim in (".codex/agents/xx-bug-auditor.toml", ".grok/agents/xx-bug-auditor.md"):
        assert b".claude/agents/xx-bug-auditor.md" in mapping[shim], f"{profile}: {shim} does not name the brief"


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_the_ship_adapter_and_its_three_references_render_and_mirror(profile):
    """The template's ship skill is an adapter plus `references/{common,plan,
    implement}.md`; every rendered profile carries all four under `.claude/`
    and byte-identical under `.agents/`, with no placeholder left."""
    mapping = _mapping(profile)
    for name in ("SKILL.md", "references/common.md", "references/plan.md",
                 "references/implement.md"):
        claude = f".claude/skills/ship/{name}"
        agents = f".agents/skills/ship/{name}"
        assert claude in mapping and agents in mapping, (profile, name)
        assert mapping[claude] == mapping[agents], (profile, name)
        assert b"{{" not in mapping[claude], (profile, name)
    adapter = mapping[".claude/skills/ship/SKILL.md"].decode()
    for name in ("common", "plan", "implement"):
        assert f"`references/{name}.md`" in adapter


# --- the shunt skill ------------------------------------------------------------

SHUNT_FILES = ("SKILL.md", "bulk_read.py", "code_write.py", "exempt.py", "workers.json")


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_the_shunt_skill_renders_and_mirrors_for_every_profile(profile):
    mapping = _mapping(profile)
    template = pack_render.vendor_dir() / "template" / ".claude" / "skills" / "shunt"
    for name in SHUNT_FILES:
        claude = mapping[f".claude/skills/shunt/{name}"]
        assert claude == template.joinpath(name).read_bytes(), name
        assert mapping[f".agents/skills/shunt/{name}"] == claude, name
    assert f".agents/skills/shunt/{pack_render.OPENAI_YAML}" in mapping
    # The wrappers run as `python3 <path>`; nothing under the skill is 0755.
    assert not {k for k in pack_render.executable_keys(mapping) if "/shunt/" in k}


def test_ships_shunt_is_the_template_skill_files_presence(tmp_path):
    assert pack_render.ships_shunt() is True
    assert pack_render.ships_shunt(tmp_path) is False
    (tmp_path / "template" / ".claude" / "skills" / "shunt").mkdir(parents=True)
    (tmp_path / "template" / ".claude" / "skills" / "shunt" / "SKILL.md").write_text("x")
    assert pack_render.ships_shunt(tmp_path) is True


def test_the_template_workers_json_is_the_shipped_table_and_a_default_render_keeps_it():
    from dark_army_daemon import card_prepare
    template = pack_render.vendor_dir() / "template" / pack_render.WORKERS_KEY
    assert json.loads(template.read_text()) == card_prepare.WORKER_MODELS
    assert pack_render.SHIPPED_WORKERS == card_prepare.WORKER_MODELS
    assert template.read_bytes() == pack_render.workers_json(None)
    # No table at all leaves the template bytes; the shipped table renders
    # byte-identically to it.
    assert _mapping("web")[pack_render.WORKERS_KEY] == template.read_bytes()
    assert _mapping("web", models=_shipped())[pack_render.WORKERS_KEY] == template.read_bytes()


def test_workers_json_is_pinned_from_the_worker_cells_before_the_mirror():
    from dark_army_daemon import agent_models
    table = agent_models.resolve(
        {"agent_models": {"codex": {"worker": "gpt-5.5"}, "claude": {"worker": ""}}},
        "/tmp/sample")
    mapping = _mapping("both", models=table)
    written = json.loads(mapping[pack_render.WORKERS_KEY])
    # A chosen cell lands; an empty (Default) cell writes the shipped worker.
    assert written == {"claude": "haiku", "codex": "gpt-5.5", "grok": "grok-4.5"}
    assert mapping[".agents/skills/shunt/workers.json"] == mapping[pack_render.WORKERS_KEY]


def test_the_three_shunt_allow_rows_are_owned():
    rows = pack_render.settings_allow_rows(_mapping("web")[pack_render.SETTINGS_KEY])
    for name in ("bulk_read", "code_write", "exempt"):
        assert f"Bash(python3 .claude/skills/shunt/{name}.py:*)" in rows
    settings = json.loads(_mapping("web")[pack_render.SETTINGS_KEY])
    assert "hooks" not in settings and "env" not in settings


def test_the_pipeline_allow_rows_are_owned_and_never_blanket():
    # The two git write rows are the card branch's commits (the person's
    # decision of 28 Sep 2026, `docs/card-worktrees.md`); no other git write.
    required = {"Bash(.venv/bin/pytest:*)", "Bash(swift build:*)",
                "Bash(swift test:*)", "Bash(npm run build:*)",
                "Bash(ruff check:*)", "mcp__dark-army", "mcp__bob",
                "Bash(git add:*)", "Bash(git commit:*)"}
    git_writes = {"Bash(git add:*)", "Bash(git commit:*)"}
    for raw in (_mapping("web")[pack_render.SETTINGS_KEY],
                (_REPO / ".claude/settings.json").read_bytes()):
        settings = json.loads(raw)
        allow = set(settings["permissions"]["allow"])
        assert required <= allow
        assert not any(row.startswith(("Edit", "Write", "Bash(git push",
                                       "Bash(git reset", "Bash(git checkout",
                                       "Bash(rm")) for row in allow)
        assert {row for row in allow if row.startswith(
            ("Bash(git add", "Bash(git commit"))} == git_writes
        assert "defaultMode" not in settings["permissions"]


_REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_template_close_out_is_the_repo_script():
    """The pack's close-out helper is Dark Army's own, byte for byte: both
    leave the terminal open by default and close only under `--close`, or
    under `--plan` when the board names the session a planning run."""
    template = (_REPO / "host" / "dark_army_menubar" / "agent_pack" / "template"
                / ".claude" / "skills" / "ship" / "close-out.sh")
    repo = _REPO / ".claude" / "skills" / "ship" / "close-out.sh"
    assert template.read_bytes() == repo.read_bytes()
    rendered = _mapping("both")[".claude/skills/ship/close-out.sh"]
    assert rendered == repo.read_bytes()


@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_template_settings_allow_both_close_out_rows(profile):
    settings = json.loads(_mapping(profile)[".claude/settings.json"])
    allow = settings["permissions"]["allow"]
    assert "Bash(bash .claude/skills/ship/close-out.sh)" in allow
    assert "Bash(bash .claude/skills/ship/close-out.sh --close)" in allow
    assert "Bash(bash .claude/skills/ship/close-out.sh --plan)" in allow
    assert not any(row.startswith("Bash(bash .claude/skills/ship/close-out.sh:")
                   for row in allow)


# --- the scout checker ------------------------------------------------------------


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_the_manual_check_checker_renders_mirrors_and_is_allowed(profile):
    """`manual_check.py` ships beside the implement reference on both trees,
    byte-equal to the daemon's module, 0644 (it runs as `python3 <path>`),
    with its own allow row in every profile's settings."""
    mapping = _mapping(profile)
    module = (_REPO / "host" / "dark_army_daemon" / "manual_check.py").read_bytes()
    for key in (".claude/skills/ship/manual_check.py",
                ".agents/skills/ship/manual_check.py"):
        assert mapping[key] == module, key
        assert key not in pack_render.executable_keys(mapping), key
    rows = pack_render.settings_allow_rows(mapping[pack_render.SETTINGS_KEY])
    assert "Bash(python3 .claude/skills/ship/manual_check.py:*)" in rows


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_the_scout_checker_renders_mirrors_and_is_allowed(profile):
    mapping = _mapping(profile)
    module = (_REPO / "host" / "dark_army_daemon" / "scout_report.py").read_bytes()
    for key in (".claude/skills/scout/scout_check.py",
                ".agents/skills/scout/scout_check.py"):
        assert mapping[key] == module, key
        assert key not in pack_render.executable_keys(mapping), key
    rows = pack_render.settings_allow_rows(mapping[pack_render.SETTINGS_KEY])
    assert "Bash(python3 .claude/skills/scout/scout_check.py:*)" in rows


@pytest.mark.parametrize("profile", pack_render.PROFILES)
def test_the_scout_skill_renders_on_both_trees_and_ship_keeps_an_alias(profile):
    mapping = _mapping(profile)
    for tree in (".claude", ".agents"):
        for name in ("SKILL.md", "references/scout.md", "scout_check.py"):
            assert f"{tree}/skills/scout/{name}" in mapping, (tree, name)
        # The old scout mode files moved out of ship.
        assert f"{tree}/skills/ship/references/scout.md" not in mapping
        assert f"{tree}/skills/ship/scout_check.py" not in mapping
    assert f".agents/skills/scout/{pack_render.OPENAI_YAML}" in mapping
    skill = mapping[".claude/skills/scout/SKILL.md"].decode()
    assert skill.startswith("---\nname: scout\n")
    assert "/scout" in skill and "investigate or scout a question" in skill
    assert "`references/scout.md`" in skill
    ship = mapping[".claude/skills/ship/SKILL.md"].decode()
    assert "`/ship scout <brief>` is an alias" in ship
    assert "`.claude/skills/scout/SKILL.md`" in ship


# --- The per-role effort, written into the briefs and the shims -------------

def _effort_lines(data: bytes) -> list[str]:
    return re.findall(r"^(?:effort:|model_reasoning_effort =).*$",
                      data.decode("utf-8"), re.M)


def _efforts():
    from dark_army_daemon import agent_models
    return agent_models.resolve_efforts({}, "/tmp/sample")


def test_effort_render_with_no_table_is_byte_identical_and_codex_still_says_high():
    assert _mapping("both") == _mapping("both", efforts=None)
    mapping = _mapping("both")
    assert _effort_lines(mapping[".codex/agents/xx-planner.toml"]) == [
        'model_reasoning_effort = "high"']
    assert _effort_lines(mapping[".claude/agents/xx-planner.md"]) == []
    assert _effort_lines(mapping[".grok/agents/xx-planner.md"]) == []


def test_effort_render_with_the_shipped_table_is_byte_identical():
    assert _mapping("both", models=_shipped(), efforts=_efforts()) == _mapping(
        "both", models=_shipped())


def test_effort_a_chosen_level_lands_in_all_three_files_where_each_cli_reads_it():
    table = _efforts()
    for provider in ("claude", "codex", "grok"):
        table[provider]["verifier"] = "low"
    mapping = _mapping("web", models=_shipped(), efforts=table)
    claude = mapping[".claude/agents/xx-verifier.md"].decode("utf-8").split("\n")
    assert claude[1] == "name: xx-verifier"
    assert claude[2] == "model: sonnet"
    assert claude[3] == "effort: low"
    assert _effort_lines(mapping[".codex/agents/xx-verifier.toml"]) == [
        'model_reasoning_effort = "low"']
    grok = mapping[".grok/agents/xx-verifier.md"].decode("utf-8").split("\n")
    at = next(i for i, line in enumerate(grok) if line.startswith("model: "))
    assert grok[at + 1] == "effort: low"
    assert grok[at + 2] == "---"


def test_effort_default_removes_the_line_in_all_three_files():
    table = _efforts()
    for provider in ("claude", "codex", "grok"):
        table[provider]["planner"] = ""
    mapping = _mapping("web", models=_shipped(), efforts=table)
    for key in (".claude/agents/xx-planner.md", ".codex/agents/xx-planner.toml",
                ".grok/agents/xx-planner.md"):
        assert _effort_lines(mapping[key]) == [], key
    # A neighbour keeps the shipped Codex level.
    assert _effort_lines(mapping[".codex/agents/xx-verifier.toml"]) == [
        'model_reasoning_effort = "high"']


def test_effort_shipped_codex_effort_is_the_agent_models_shipped_level():
    from dark_army_daemon import agent_models
    for role in agent_models.EFFORT_SLOTS[1:]:
        assert pack_render.SHIPPED_CODEX_EFFORT == (
            agent_models.SHIPPED_EFFORTS["codex"][role])


def test_effort_pin_effort_replaces_rather_than_adding_a_second_line():
    text = "---\nname: xx-planner\nmodel: opus\neffort: low\ndescription: d\n---\n\nbody\n"
    pinned = pack_render.pin_effort(text, "high")
    assert pinned.count("effort:") == 1
    assert pinned == ("---\nname: xx-planner\nmodel: opus\neffort: high\n"
                      "description: d\n---\n\nbody\n")
    assert pack_render.pin_effort(pinned, "") == (
        "---\nname: xx-planner\nmodel: opus\ndescription: d\n---\n\nbody\n")
    assert pack_render.pin_effort("no frontmatter\n", "low") == "no frontmatter\n"
    # No model line: after the name.
    assert pack_render.pin_effort("---\nname: n\ndescription: d\n---\nb", "low") == (
        "---\nname: n\neffort: low\ndescription: d\n---\nb")


def test_effort_pin_toml_effort_replaces_inserts_and_removes():
    shim = pack_render.codex_shim("xx-a", {"description": "d"}, "read-only",
                                  model="gpt-6-sol")
    low = pack_render.pin_toml_effort(shim, "low")
    assert low.count("model_reasoning_effort") == 1
    assert 'model_reasoning_effort = "low"' in low
    assert pack_render.pin_toml_effort(shim, "high") == shim
    removed = pack_render.pin_toml_effort(shim, "")
    assert "model_reasoning_effort" not in removed
    assert pack_render.pin_toml_effort(removed, "high") == shim
    assert pack_render.pin_toml_effort(removed, "") == removed
    assert pack_render.codex_shim("xx-a", {"description": "d"}, "read-only",
                                  model="gpt-6-sol", effort="") == removed
