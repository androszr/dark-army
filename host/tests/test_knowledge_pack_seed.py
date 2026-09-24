# host/tests/test_knowledge_pack_seed.py
"""Seed-once: the pack writes the knowledge skill into a project that does not
have it, and never again.

The whole install path runs — `pack_render.render` really renders,
`pack_install.install_pack` really writes into a temp root, and the second
install really leaves an edited file alone. A mocked `_write_one` would prove
nothing about the branch's position in `_write_render`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dark_army_daemon import enrollment
from dark_army_menubar import pack_install, pack_ledger, pack_render

SKILL = ".claude/skills/knowledge/SKILL.md"
QUESTIONS = ".claude/skills/knowledge/questions.md"
MIRROR = ".agents/skills/knowledge/SKILL.md"

CANONICAL = (Path(pack_render.vendor_dir()) / "template"
             / ".claude/skills/knowledge")


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


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    return root, folder


def _install(folder: str):
    ok, detail, owned = pack_install.install_pack(
        folder, "web", "xx", "sample-app")
    assert ok, detail
    pack_ledger.remember(
        folder, profile="web", prefix="xx", project="sample-app",
        settings_allow_owned=owned, last_result="ok")
    return owned


# --- the seed -----------------------------------------------------------------


def test_a_fresh_project_receives_the_skill_and_the_catalogue(project):
    root, folder = project
    _install(folder)
    assert (root / SKILL).is_file()
    assert (root / QUESTIONS).is_file()


def test_what_lands_is_byte_identical_to_the_shipped_pair(project):
    """The success criterion's second sentence: *a second project can install
    and use it without modification*. No substitution runs over either file."""
    root, folder = project
    _install(folder)
    assert (root / SKILL).read_bytes() == (CANONICAL / "SKILL.md").read_bytes()
    assert (root / QUESTIONS).read_bytes() == (
        CANONICAL / "questions.md").read_bytes()


def test_neither_shipped_file_carries_a_placeholder():
    for name in ("SKILL.md", "questions.md"):
        assert b"{{" not in (CANONICAL / name).read_bytes(), name


# --- and never again -----------------------------------------------------------


def test_a_second_install_leaves_an_edited_copy_alone(project):
    root, folder = project
    _install(folder)
    (root / SKILL).write_text("# our own interview\n", encoding="utf-8")
    (root / QUESTIONS).write_text("key: `ours`\n", encoding="utf-8")

    _install(folder)

    assert (root / SKILL).read_text(encoding="utf-8") == "# our own interview\n"
    assert (root / QUESTIONS).read_text(encoding="utf-8") == "key: `ours`\n"


def test_the_other_rendered_keys_still_update(project):
    """Seed-once must be one branch about two files, not a resync that stopped
    working."""
    root, folder = project
    _install(folder)
    managed = root / "CLAUDE.md"
    original = managed.read_bytes()
    managed.write_bytes(b"clobbered\n")
    (root / SKILL).write_text("# ours\n", encoding="utf-8")

    _install(folder)

    assert managed.read_bytes() != b"clobbered\n"
    assert pack_render.BEGIN_MARK.encode("utf-8") in managed.read_bytes()
    assert original  # the managed file had content to begin with
    assert (root / SKILL).read_text(encoding="utf-8") == "# ours\n"


def test_the_agents_mirror_is_seeded_once_too(project):
    """`pack_render.mirror_skills` synthesises the `.agents` copy from the
    `.claude` one: without the mirror keys in `SEED_ONCE_KEYS` a resync would
    rewrite it from the shipped text, which is the same clobber one directory
    over."""
    root, folder = project
    _install(folder)
    assert (root / MIRROR).is_file()
    (root / MIRROR).write_text("# ours, mirrored\n", encoding="utf-8")

    _install(folder)

    assert (root / MIRROR).read_text(encoding="utf-8") == "# ours, mirrored\n"


def test_unlink_strays_leaves_the_edited_mirror_in_place(project):
    """The prune spares any key present in the rendered mapping (`rel in
    expected`), and the seed keys are rendered — this pins that, because the
    prune is the only other thing that could remove the file."""
    root, folder = project
    _install(folder)
    (root / MIRROR).write_text("# ours, mirrored\n", encoding="utf-8")

    mapping = pack_render.render("web", "xx", "sample-app")
    pack_install._unlink_strays(folder, mapping)

    assert (root / MIRROR).read_text(encoding="utf-8") == "# ours, mirrored\n"


# --- the constant itself -------------------------------------------------------


def test_every_seed_key_is_actually_rendered():
    """A renamed skill file can never silently stop being seeded: a key in
    `SEED_ONCE_KEYS` that the renderer does not produce protects nothing."""
    mapping = pack_render.render("web", "xx", "sample-app")
    missing = sorted(pack_install.SEED_ONCE_KEYS - set(mapping))
    assert missing == [], missing


def test_a_directory_at_the_destination_counts_as_present(project):
    """`exists()` rather than `is_file()`: a directory or a symlink sitting at
    that path is also 'already present' and must not be replaced."""
    root, folder = project
    (root / SKILL).parent.mkdir(parents=True)
    (root / SKILL).mkdir()
    _install(folder)
    assert (root / SKILL).is_dir(), "a directory must not be replaced by a file"


def test_a_symlink_to_a_real_file_is_present_and_is_left_alone(project):
    """`_destination` realpaths, so both the seed check and the write see the
    link's *target*: a link pointing at something that exists reads as present
    and is not replaced — the link and its target both survive."""
    root, folder = project
    (root / SKILL).parent.mkdir(parents=True)
    target = root / ".claude/skills/knowledge/ours.md"
    target.write_text("# ours\n", encoding="utf-8")
    (root / SKILL).symlink_to(target)

    _install(folder)

    assert (root / SKILL).is_symlink()
    assert target.read_text(encoding="utf-8") == "# ours\n"


def test_a_broken_symlink_is_the_recorded_gap(project):
    """The one gap, recorded rather than left accidental. Both `exists()` and
    `is_file()` call a broken link absent, and `_destination` has already
    resolved the path to the link's missing target — so the seed proceeds and
    writes *that* file, leaving the link resolvable. It stays inside the
    project (`_destination` refuses an escape), which is what bounds it; a
    broken link in a skill folder is already a project that needs a person."""
    root, folder = project
    (root / SKILL).parent.mkdir(parents=True)
    target = root / ".claude/skills/knowledge/nowhere.md"
    (root / SKILL).symlink_to(target)

    _install(folder)

    assert target.is_file()
    assert target.read_bytes() == (CANONICAL / "SKILL.md").read_bytes()
    assert (root / SKILL).is_symlink()


def test_the_shunt_skill_is_rewritten_not_seeded_once(project):
    """The shunt files are ordinary owned pack output: unlike the knowledge
    pair, an edited copy is put back on the next install."""
    root, folder = project
    _install(folder)
    skill = root / ".claude/skills/shunt/SKILL.md"
    workers = root / ".claude/skills/shunt/workers.json"
    original = skill.read_bytes()
    skill.write_text("# ours\n", encoding="utf-8")
    workers.write_text("{}\n", encoding="utf-8")
    _install(folder)
    assert skill.read_bytes() == original
    assert workers.read_text(encoding="utf-8") != "{}\n"
    assert (root / ".agents/skills/shunt/SKILL.md").read_bytes() == original
