"""A guard script or workflow the project adapted belongs to the project.

The iOS profile seeds `scripts/check-*.py` and `.github/workflows/*.yml`, and
every project adapts them: which Debug entitlements file it has, which privacy
keys it can reach. Before 24 Sep 2026 every resync wrote the shipped copy back
over those edits; arpg-web's gate went red on two scripts nobody in it had
touched. The pack now rewrites such a file only while it is still the bytes
the pack last wrote there.
"""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon import enrollment
from dark_army_menubar import pack_install, pack_ledger, pack_render

SCRIPT = "scripts/check-entitlements.py"
WORKFLOW = ".github/workflows/ios.yml"


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


def _project(tmp_path, monkeypatch, *, before=None):
    root = tmp_path / "proj"
    root.mkdir()
    for key, text in (before or {}).items():
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(text)
    monkeypatch.setattr(
        "dark_army_daemon.paths.AGENT_PACK_PATH",
        tmp_path / "agent-pack.json",
    )
    folder = _admit(monkeypatch, root)
    ok, detail, _owned = pack_install.install_pack(
        folder, "ios", "xx", "SampleApp")
    assert ok, detail
    return root, folder


def _resync(folder):
    pack_install._resync_one(
        folder, dict(pack_ledger.entry(folder)), pack_install.pack_root())


def _newer(monkeypatch, key):
    real = pack_render.render

    def newer(*args, **kwargs):
        mapping = dict(real(*args, **kwargs))
        mapping[key] = mapping[key] + b"\n# a newer shipped line\n"
        return mapping

    monkeypatch.setattr(pack_render, "render", newer)
    return real


def test_scripts_and_workflows_are_project_adapted():
    assert pack_render.project_adapted(SCRIPT)
    assert pack_render.project_adapted(WORKFLOW)
    assert not pack_render.project_adapted(".claude/skills/ship/SKILL.md")
    assert not pack_render.project_adapted("docs/context.md")


def test_install_records_what_it_wrote(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    digests = pack_ledger.digests_of(pack_ledger.entry(folder))
    for key in (SCRIPT, WORKFLOW):
        assert digests[key] == pack_render.file_digest(
            (root / key).read_bytes())


def test_resync_leaves_an_adapted_script_alone(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    adapted = (root / SCRIPT).read_bytes() + b"\n# ours\n"
    (root / SCRIPT).write_bytes(adapted)
    _newer(monkeypatch, SCRIPT)
    _resync(folder)
    assert (root / SCRIPT).read_bytes() == adapted
    pack_install.install_pack(folder, "ios", "xx", "SampleApp")
    assert (root / SCRIPT).read_bytes() == adapted


def test_an_untouched_script_still_takes_a_newer_one(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    real = _newer(monkeypatch, WORKFLOW)
    _resync(folder)
    assert b"a newer shipped line" in (root / WORKFLOW).read_bytes()
    # ...and it is still the pack's, so the next shipped version lands too.
    monkeypatch.setattr(pack_render, "render", real)
    _resync(folder)
    assert b"a newer shipped line" not in (root / WORKFLOW).read_bytes()


def test_a_project_script_present_before_install_is_kept(
        tmp_path, monkeypatch):
    own = b"#!/usr/bin/env python3\nprint('ours')\n"
    root, _folder = _project(tmp_path, monkeypatch, before={SCRIPT: own})
    assert (root / SCRIPT).read_bytes() == own


def test_a_row_from_an_older_build_keeps_a_differing_script(
        tmp_path, monkeypatch):
    # arpg-web's case: the ledger row predates the whole-file digests.
    root, folder = _project(tmp_path, monkeypatch)
    adapted = (root / SCRIPT).read_bytes() + b"\n# ours\n"
    (root / SCRIPT).write_bytes(adapted)
    pack_ledger.update(folder, pack_digests={})
    _resync(folder)
    assert (root / SCRIPT).read_bytes() == adapted


def test_a_deleted_script_is_seeded_again(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    shipped = (root / SCRIPT).read_bytes()
    (root / SCRIPT).write_bytes(shipped + b"\n# ours\n")
    (root / SCRIPT).unlink()
    _resync(folder)
    assert (root / SCRIPT).read_bytes() == shipped


def test_crlf_checkout_reads_as_unedited():
    assert pack_render.file_digest(b"a\nb\n") == pack_render.file_digest(
        b"a\r\nb\r\n")
