"""The pack's crew, its profiles and its area briefs (22 Sep 2026).

Every rendered project gets the generic integration and security reviewers;
a profile is any folder holding a `profile.json`, the person's own under
`~/.dark-army/profiles/` included; a profile picks which area briefs ship,
and a resync removes only a brief whose bytes the pack itself once wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from dark_army_daemon import enrollment
from dark_army_menubar import pack_install, pack_render

REPO = Path(__file__).resolve().parents[2]


def _admit(monkeypatch, root: Path) -> str:
    folder = enrollment.normalise(str(root))
    monkeypatch.setattr(
        enrollment, "root_enrolled",
        lambda cwd: folder if cwd and enrollment.normalise(cwd) == folder else "")
    return folder


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    mine = tmp_path / "my-profiles"
    mine.mkdir()
    monkeypatch.setattr("dark_army_daemon.paths.AGENT_PACK_PATH",
                        tmp_path / "agent-pack.json")
    monkeypatch.setattr("dark_army_daemon.paths.USER_PROFILES_PATH", mine)
    return root, _admit(monkeypatch, root), mine


def _profile(folder: Path, name: str, data) -> Path:
    home = folder / name
    home.mkdir(parents=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (home / "profile.json").write_text(text, encoding="utf-8")
    return home


def _reviewer_rows(mapping) -> str:
    return mapping["docs/context.md"].decode("utf-8")


# --- 1. the crew: seven briefs on every profile --------------------------

@pytest.mark.parametrize("profile", ["web", "ios", "both"])
def test_every_profile_ships_both_generic_checkers(profile):
    mapping = pack_render.render(profile, "xx", "sample-app")
    for role in ("integration-reviewer", "security-reviewer"):
        assert f".claude/agents/xx-{role}.md" in mapping
        assert f".codex/agents/xx-{role}.toml" in mapping
        assert f".grok/agents/xx-{role}.md" in mapping
        assert f"`xx-{role}`" in _reviewer_rows(mapping), (profile, role)
    for role in ("planner", "implementer", "verifier", "bug-auditor",
                 "card-preparer"):
        assert f".claude/agents/xx-{role}.md" in mapping


def test_the_generic_checkers_are_read_only_and_name_no_other_project():
    mapping = pack_render.render("ios", "xx", "sample-app")
    for role in ("integration-reviewer", "security-reviewer"):
        text = mapping[f".claude/agents/xx-{role}.md"].decode("utf-8")
        fm = pack_render._frontmatter(text)
        assert pack_render.sandbox_for(fm) == "read-only"
        assert "sample-app" in fm["description"]
        assert "Dark Army" not in text and "bc-" not in text
        assert 'sandbox_mode = "read-only"' in mapping[
            f".codex/agents/xx-{role}.toml"].decode("utf-8")


def test_the_web_profile_keeps_its_own_security_reviewer():
    web = pack_render.render("web", "xx", "sample-app")
    ios = pack_render.render("ios", "xx", "sample-app")
    assert "NEXT_PUBLIC" in web[".claude/agents/xx-security-reviewer.md"].decode()
    assert "NEXT_PUBLIC" not in ios[".claude/agents/xx-security-reviewer.md"].decode()


# --- 2. profiles are folders, the person's own included ------------------

def test_valid_profile_id_touches_nothing_and_refuses_paths():
    for good in ("web", "ios", "both", "python-cli", "web-next-vercel"):
        assert pack_render.valid_profile_id(good)
    for bad in ("", "a", "../x", "Web", "x/y", "-x", "a" * 41, None):
        assert not pack_render.valid_profile_id(bad)


def test_a_one_line_profile_of_your_own_renders_with_defaults(tmp_path):
    mine = tmp_path / "mine"
    _profile(mine, "python-cli", {"summary": "A Python command-line tool."})
    listed = pack_render.available_profiles(user_dir=mine)
    assert [row["id"] for row in listed][:3] == ["web", "ios", "both"]
    row = next(r for r in listed if r["id"] == "python-cli")
    assert row == {"id": "python-cli", "label": "python-cli",
                   "summary": "A Python command-line tool.", "origin": "yours"}
    mapping = pack_render.render("python-cli", "pc", "tool", user_dir=mine)
    rows = _reviewer_rows(mapping)
    assert "`pc-security-reviewer`" in rows and "`pc-integration-reviewer`" in rows
    assert "reviewer_agent: pc-security-reviewer" in mapping[
        ".claude/review.md"].decode()
    leads = {Path(k).stem for k in mapping if k.startswith(".claude/leads/")}
    assert len(leads) == 8  # no `areas`: every brief ships
    json.loads(mapping[".claude/settings.json"])


def test_your_profile_needs_the_user_dir_to_render(tmp_path):
    mine = tmp_path / "mine"
    _profile(mine, "python-cli", {})
    with pytest.raises(pack_render.PackRenderError, match="unknown profile"):
        pack_render.render("python-cli", "pc", "tool")


def test_an_allow_row_with_a_quote_cannot_break_settings(tmp_path):
    mine = tmp_path / "mine"
    row = 'Bash(echo "hi"), "extra": true'
    _profile(mine, "quoted", {"settings_allow": [row]})
    mapping = pack_render.render("quoted", "qq", "tool", user_dir=mine)
    settings = json.loads(mapping[".claude/settings.json"])
    assert row in settings["permissions"]["allow"]
    assert "extra" not in settings


def test_shipped_allow_rows_render_byte_identical_to_before():
    text = pack_render.render("web", "xx", "sample-app")[
        ".claude/settings.json"].decode()
    assert '      "Bash(pnpm test:*)"' in text


def test_a_shipped_name_is_never_read_from_your_folder(tmp_path):
    mine = tmp_path / "mine"
    _profile(mine, "web-next-vercel", {"summary": "impostor"})
    _profile(mine, "web", {"summary": "impostor"})
    listed = pack_render.available_profiles(user_dir=mine)
    assert all(row["summary"] != "impostor" for row in listed)
    mapping = pack_render.render("web", "xx", "app", user_dir=mine)
    assert "pnpm lint" in mapping["docs/context.md"].decode()


def test_a_broken_or_linked_profile_is_left_out(tmp_path):
    mine = tmp_path / "mine"
    _profile(mine, "broken", "{not json")
    _profile(mine, "wrongshape", {"gates": "pnpm test"})
    real = _profile(tmp_path / "elsewhere", "real", {})
    os.symlink(real, mine / "linked")
    ids = {row["id"] for row in pack_render.available_profiles(user_dir=mine)}
    assert not ids & {"broken", "wrongshape", "linked"}
    with pytest.raises(pack_render.PackRenderError, match="could not be read"):
        pack_render.render("broken", "xx", "app", user_dir=mine)
    with pytest.raises(pack_render.PackRenderError, match="gates"):
        pack_render.render("wrongshape", "xx", "app", user_dir=mine)
    with pytest.raises(pack_render.PackRenderError, match="unknown profile"):
        pack_render.render("linked", "xx", "app", user_dir=mine)


def test_a_symlink_inside_your_profile_is_not_copied(tmp_path):
    mine = tmp_path / "mine"
    home = _profile(mine, "linky", {})
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy", encoding="utf-8")
    (home / "scripts").mkdir()
    os.symlink(secret, home / "scripts" / "leak.py")
    (home / "scripts" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    mapping = pack_render.render("linky", "xx", "app", user_dir=mine)
    assert "scripts/ok.py" in mapping
    assert "scripts/leak.py" not in mapping


def test_a_linked_folder_or_preflight_in_your_profile_is_not_copied(tmp_path):
    mine = tmp_path / "mine"
    home = _profile(mine, "linked", {})
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "x.md").write_text("do not copy", encoding="utf-8")
    (outside / "gate.md").write_text("do not copy", encoding="utf-8")
    (outside / "hunt.md").write_text("do not copy", encoding="utf-8")
    os.symlink(outside, home / "scripts")
    os.symlink(outside, home / "fragments")
    os.symlink(outside / "gate.md", home / "PREFLIGHT-x.md")
    (home / "PREFLIGHT-ok.md").write_text("own gate\n", encoding="utf-8")
    mapping = pack_render.render("linked", "xx", "app", user_dir=mine)
    assert not any(k.startswith("scripts/sub/") for k in mapping)
    assert not any(k.endswith("PREFLIGHT-x.md") for k in mapping)
    assert any(k.endswith("PREFLIGHT-ok.md") for k in mapping)
    assert all(b"do not copy" not in body for body in mapping.values())


def test_the_listing_follows_an_edited_profile(tmp_path):
    mine = tmp_path / "mine"
    home = _profile(mine, "mine-one", {"summary": "first"})
    assert any(r["summary"] == "first"
               for r in pack_render.available_profiles(user_dir=mine))
    manifest = home / "profile.json"
    manifest.write_text(json.dumps({"summary": "second"}), encoding="utf-8")
    stamp = manifest.stat().st_mtime_ns + 1_000_000_000
    os.utime(manifest, ns=(stamp, stamp))
    assert any(r["summary"] == "second"
               for r in pack_render.available_profiles(user_dir=mine))


def test_install_and_resync_reach_your_profile(project):
    root, folder, mine = project
    _profile(mine, "python-cli", {"gates": [["test_gate", "pytest -q"]],
                                  "areas": ["backbone"]})
    ok, detail, _ = pack_install.install_pack(folder, "python-cli", "pc", "tool")
    assert ok, detail
    assert "pytest -q" in (root / "docs/context.md").read_text()
    context = root / "docs/context.md"
    context.write_text(context.read_text().replace("pytest -q", "gone"))
    pack_install.resync_all()
    assert "pytest -q" in context.read_text()


# --- 3. area briefs: project-neutral, chosen by the profile ---------------

def test_shipped_leads_name_nothing_of_dark_army():
    for path in (REPO / "host/dark_army_menubar/agent_pack/template/.claude/leads"
                 ).glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "Dark Army" not in text, path.name
        assert "docs/phone-contract.md" not in text, path.name


def test_a_profile_can_overlay_its_own_lead(tmp_path):
    mine = tmp_path / "mine"
    home = _profile(mine, "gamey", {"areas": ["play"]})
    (home / "leads").mkdir()
    (home / "leads" / "play.md").write_text(
        "# Play — this game's feel\n", encoding="utf-8")
    mapping = pack_render.render("gamey", "gg", "game", user_dir=mine)
    leads = {Path(k).stem for k in mapping if k.startswith(".claude/leads/")}
    assert leads == {"play", "universal"}
    assert mapping[".claude/leads/play.md"] == b"# Play \xe2\x80\x94 this game's feel\n"


def test_dark_armys_own_leads_are_in_the_legacy_digests():
    """The checkout's own briefs are byte-for-byte what earlier packs
    shipped, so a project still holding one is recognised as untouched."""
    for path in (REPO / ".claude/leads").glob("*.md"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in pack_render.LEGACY_LEAD_DIGESTS, path.name


def test_resync_removes_an_untouched_old_lead_and_keeps_yours(project):
    root, folder, _mine = project
    leads = root / ".claude" / "leads"
    leads.mkdir(parents=True)
    # What the pack wrote before: Dark Army's own copy, byte for byte.
    (leads / "desk.md").write_bytes((REPO / ".claude/leads/desk.md").read_bytes())
    (leads / "play.md").write_text("# Play — my own notes\n", encoding="utf-8")
    (leads / "notes.md").write_text("mine\n", encoding="utf-8")
    ok, detail, _ = pack_install.install_pack(folder, "ios", "xx", "app")
    assert ok, detail
    assert not (leads / "desk.md").exists()
    assert (leads / "play.md").read_text(encoding="utf-8") == "# Play — my own notes\n"
    assert (leads / "notes.md").is_file()
    assert (leads / "pocket.md").is_file()
    assert "Dark Army" not in (leads / "pocket.md").read_text(encoding="utf-8")


def test_a_profile_switch_drops_the_briefs_it_no_longer_ships(project):
    root, folder, _mine = project
    ok, detail, _ = pack_install.install_pack(folder, "ios", "xx", "app")
    assert ok, detail
    assert (root / ".claude/leads/pocket.md").is_file()
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "app")
    assert ok, detail
    assert not (root / ".claude/leads/pocket.md").exists()
    assert (root / ".claude/leads/ledger.md").is_file()


def test_a_linked_leads_folder_is_never_pruned(project, tmp_path):
    root, folder, _mine = project
    outside = tmp_path / "outside-leads"
    outside.mkdir()
    (outside / "desk.md").write_bytes((REPO / ".claude/leads/desk.md").read_bytes())
    (root / ".claude").mkdir()
    os.symlink(outside, root / ".claude" / "leads")
    pack_install._unlink_stale_leads(folder, {}, pack_install.pack_root())
    assert (outside / "desk.md").is_file()
