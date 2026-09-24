"""The starter .gitignore the agent pack offers a project.

Pure merge rules first (`pack_gitignore`), then the install path on a
throwaway project: created in a git repository and nowhere else, a
project's own lines kept byte for byte, offered once per line, idempotent
on a second install and on the launch resync, and never failing the rest
of the pack.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from dark_army_daemon import enrollment, paths
from dark_army_menubar import pack_gitignore, pack_install, pack_ledger, pack_render

HEADER = pack_gitignore.HEADER


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "AGENT_PACK_PATH", tmp_path / "agent-pack.json")
    monkeypatch.setattr(paths, "USER_PROFILES_PATH", tmp_path / "profiles")


def _admit(monkeypatch, root: Path) -> str:
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


def _project(tmp_path: Path, monkeypatch, git: bool = True) -> tuple[Path, str]:
    root = tmp_path / "proj"
    root.mkdir()
    if git:
        (root / ".git").mkdir()
    return root, _admit(monkeypatch, root)


def _forms(text: str) -> list[str]:
    return [f for f in (pack_gitignore.canonical(x) for x in text.splitlines()) if f]


def _offered_row(folder: str) -> list[str]:
    row = pack_ledger.entry(folder) or {}
    return list(row.get("gitignore_offered") or [])


# ── canonical ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", ["/plans/", "plans", "plans/ ", "**/plans/", "plans/\r"])
def test_canonical_equivalent_forms(line):
    assert pack_gitignore.canonical(line) == "plans"


@pytest.mark.parametrize("line", ["# note", "", "   "])
def test_canonical_blank_and_comment(line):
    assert pack_gitignore.canonical(line) == ""


def test_canonical_keeps_a_negation():
    assert pack_gitignore.canonical("!.env.example") == "!.env.example"
    assert pack_gitignore.canonical("!/plans/") == "!plans"


# ── merge ────────────────────────────────────────────────────────────────────

def test_merge_empty_text_writes_header_and_every_line():
    text, appended, now = pack_gitignore.merge("", [".env", "plans/"], set())
    assert text == f"{HEADER}\n.env\nplans/\n"
    assert appended == [".env", "plans/"]
    assert now == [".env", "plans"]


def test_merge_inserts_newline_before_blank_and_header():
    text, _, _ = pack_gitignore.merge("mine", ["plans/"], set())
    assert text == f"mine\n\n{HEADER}\nplans/\n"


def test_merge_extends_an_existing_block_without_a_second_header():
    existing = f"mine\n\n{HEADER}\n.env\n\nafter\n"
    text, appended, _ = pack_gitignore.merge(existing, [".env", "plans/"], {".env"})
    assert appended == ["plans/"]
    assert text == f"mine\n\n{HEADER}\n.env\nplans/\n\nafter\n"
    assert text.count(HEADER) == 1


def test_merge_skips_a_line_already_offered_even_when_absent():
    text, appended, now = pack_gitignore.merge("mine\n", ["plans/"], {"plans"})
    assert appended == []
    assert text == "mine\n"
    assert now == ["plans"]


def test_merge_respects_a_deliberate_unignore():
    _, appended, now = pack_gitignore.merge("!.env\n", [".env"], set())
    assert appended == []
    assert now == [".env"]


def test_merge_never_reignores_a_file_the_project_unignored():
    existing = ".env*\n!.env.production\n"
    _, appended, now = pack_gitignore.merge(existing, [".env", ".env.*"], set())
    assert appended == [".env"]
    assert now == [".env", ".env.*"]


def test_merge_unignore_at_depth_blocks_a_slashless_pattern():
    _, appended, _ = pack_gitignore.merge(
        "!config/.env.local\n", [".env.*"], set())
    assert appended == []


def test_merge_always_appends_a_negation():
    _, appended, _ = pack_gitignore.merge(
        "!.env.example\n", [".env.*", "!.env.example"], set())
    assert appended == [".env.*", "!.env.example"]


def test_merge_nothing_to_add_returns_text_unchanged():
    existing = "/plans/\r\nnode_modules \r\n"
    text, appended, _ = pack_gitignore.merge(existing, ["plans/", "node_modules/"], set())
    assert appended == []
    assert text is existing


# ── the install path ─────────────────────────────────────────────────────────

def test_fresh_repo_gets_the_block(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(HEADER + "\n")
    assert text.count(HEADER) == 1
    lines = text.splitlines()
    for wanted in (".env", "!.env.example", "plans/", "docs/research/",
                   "node_modules/", ".DS_Store"):
        assert wanted in lines
    assert lines.index(".env.*") < lines.index("!.env.example")
    offered = pack_render.gitignore_lines("web")
    assert _offered_row(folder) == sorted({pack_gitignore.canonical(x) for x in offered})
    assert pack_ledger.entry(folder)["last_result"] == "ok"


def test_existing_file_lines_kept_and_not_doubled(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    original = "/plans/\nnode_modules/ \n.DS_Store\n# mine"
    (root / ".gitignore").write_text(original, encoding="utf-8")
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(original)
    forms = _forms(text)
    for form in ("plans", "node_modules", ".DS_Store"):
        assert forms.count(form) == 1, form
    tail = text[len(original):]
    assert HEADER in tail
    assert ".env" in tail.splitlines()
    assert "docs/research/" in tail.splitlines()


def test_second_install_and_resync_change_nothing(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    target = root / ".gitignore"
    before = (target.stat().st_mtime_ns, target.read_bytes())
    offered = _offered_row(folder)
    time.sleep(0.05)
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    pack_install.resync_all()
    assert (target.stat().st_mtime_ns, target.read_bytes()) == before
    assert _offered_row(folder) == offered
    assert pack_ledger.entry(folder)["last_result"] == "ok"


def test_non_git_folder_gets_no_file(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch, git=False)
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    assert not (root / ".gitignore").exists()
    assert _offered_row(folder) == []
    pack_install.resync_all()
    assert not (root / ".gitignore").exists()


def test_non_git_folder_gets_the_block_once_it_is_a_repository(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch, git=False)
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    (root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    pack_install.resync_all()
    assert HEADER in (root / ".gitignore").read_text(encoding="utf-8")


def test_offer_once_a_deleted_line_stays_deleted_on_resync(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    target = root / ".gitignore"
    kept = [x for x in target.read_text(encoding="utf-8").splitlines() if x != "plans/"]
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    offered = _offered_row(folder)
    pack_install.resync_all()
    assert "plans/" not in target.read_text(encoding="utf-8").splitlines()
    assert _offered_row(folder) == offered


def test_new_shipped_line_is_added_on_the_next_resync(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    target = root / ".gitignore"
    before = target.read_text(encoding="utf-8")
    offered = _offered_row(folder)
    base = pack_render.gitignore_lines("web")
    monkeypatch.setattr(pack_render, "gitignore_lines",
                        lambda *a, **k: base + ["extra/"])
    pack_install.resync_all()
    after = target.read_text(encoding="utf-8")
    assert after == before + "extra/\n"
    assert after.count(HEADER) == 1
    assert _offered_row(folder) == sorted(offered + ["extra"])


def test_both_profiles_contribute_each_line_once(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    ok, detail, _ = pack_install.install_pack(folder, "both", "xx", "sample-app")
    assert ok, detail
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for wanted in (".next/", ".vercel", "coverage/", "xcuserdata/",
                   "*.xcuserstate", "*.ipa", ".env", "plans/"):
        assert lines.count(wanted) == 1, wanted


def test_own_profile_contributes_its_lines_and_a_link_is_ignored(tmp_path):
    mine = tmp_path / "profiles" / "mine"
    mine.mkdir(parents=True)
    (mine / "profile.json").write_text("{}", encoding="utf-8")
    (mine / "gitignore.txt").write_text("# mine\nscratch-mine/\n.env\n",
                                        encoding="utf-8")
    lines = pack_render.gitignore_lines("mine", user_dir=tmp_path / "profiles")
    assert lines.count("scratch-mine/") == 1
    assert lines.count(".env") == 1
    assert lines.index(".env") < lines.index("scratch-mine/")

    linked = tmp_path / "profiles" / "linked"
    linked.mkdir()
    (linked / "profile.json").write_text("{}", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("leak-through-link/\n", encoding="utf-8")
    (linked / "gitignore.txt").symlink_to(elsewhere)
    lines = pack_render.gitignore_lines("linked", user_dir=tmp_path / "profiles")
    assert "leak-through-link/" not in lines
    assert "plans/" in lines


def test_unknown_profile_raises_like_render():
    with pytest.raises(pack_render.PackRenderError):
        pack_render.gitignore_lines("no-such-profile")


def test_unwritable_gitignore_keeps_the_pack_and_says_so(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    root, folder = _project(tmp_path, monkeypatch)
    target = root / ".gitignore"
    target.write_text("mine\n", encoding="utf-8")
    real = pack_render.gitignore_lines
    # First pass lays the pack down with nothing to offer, so the second
    # pass needs no new file in the (then read-only) project folder.
    monkeypatch.setattr(pack_render, "gitignore_lines", lambda *a, **k: [])
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    monkeypatch.setattr(pack_render, "gitignore_lines", real)
    target.chmod(0o444)
    root.chmod(0o555)
    try:
        ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    finally:
        root.chmod(0o755)
        target.chmod(0o644)
    assert ok, detail
    assert ".gitignore" in detail
    row = pack_ledger.entry(folder)
    assert row["last_result"].startswith("ok; ")
    assert ".gitignore" in row["last_result"]
    assert list(row.get("gitignore_offered") or []) == []
    assert target.read_text(encoding="utf-8") == "mine\n"
    for key in pack_render.render("web", "xx", "sample-app"):
        assert (root / key).is_file(), key


def test_unreadable_gitignore_is_a_note_and_offers_again(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    (root / ".gitignore").mkdir()
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    assert "could not read .gitignore" in detail
    assert _offered_row(folder) == []
    (root / ".gitignore").rmdir()
    pack_install.resync_all()
    assert HEADER in (root / ".gitignore").read_text(encoding="utf-8")
    assert pack_ledger.entry(folder)["last_result"] == "ok"


def test_self_refusal_writes_no_gitignore(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(pack_install, "_is_bobs_own", lambda r: True)
    ok, _detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert not ok
    assert not (root / ".gitignore").exists()


def test_existing_mode_is_preserved(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    target = root / ".gitignore"
    target.write_text("mine\n", encoding="utf-8")
    target.chmod(0o600)
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    assert HEADER in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_new_file_lands_0644(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    assert pack_install.install_pack(folder, "web", "xx", "sample-app")[0]
    assert stat.S_IMODE((root / ".gitignore").stat().st_mode) == 0o644


def test_a_linked_gitignore_is_never_followed(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    hooks = root / ".git" / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    link = root / ".gitignore"
    link.symlink_to(hook)
    ok, detail, _ = pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert ok, detail
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    assert stat.S_IMODE(hook.stat().st_mode) == 0o755
    assert link.is_symlink()
    assert os.readlink(link) == str(hook)
    row = pack_ledger.entry(folder)
    assert list(row.get("gitignore_offered") or []) == []
    assert row["last_result"].startswith("ok; ")
