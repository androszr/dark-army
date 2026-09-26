"""A filled-in `docs/context.md` belongs to the project, not to the pack.

The template is a page of blanks (the Identity table's gates, the
architecture, the conventions) that lives wholly between the pack markers.
Before 24 Sep 2026 every resync re-spliced it and put the blanks back over a
project's answers; vir-sunset's agents were then told to run `pnpm build`.
The pack now rewrites that region only while it is still the bytes the pack
last wrote there.
"""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon import enrollment
from dark_army_menubar import pack_install, pack_ledger, pack_render

KEY = "docs/context.md"


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


def _project(tmp_path, monkeypatch):
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
    return root, folder


def _resync(folder):
    pack_install._resync_one(
        folder, dict(pack_ledger.entry(folder)), pack_install.pack_root())


def _fill_identity(path: Path) -> bytes:
    text = path.read_text()
    filled = text.replace("| `lint_gate` | `pnpm lint` |",
                          "| `lint_gate` | `npm run lint` |")
    assert filled != text, "the web profile's lint row moved; update the test"
    path.write_text(filled)
    return filled.encode()


def test_install_records_the_region_it_wrote(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    digests = pack_ledger.digests_of(pack_ledger.entry(folder))
    assert digests[KEY] == pack_render.managed_digest(
        (root / KEY).read_bytes())


def test_resync_leaves_a_filled_in_context_alone(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    filled = _fill_identity(root / KEY)
    _resync(folder)
    assert (root / KEY).read_bytes() == filled
    pack_install.install_pack(folder, "web", "xx", "sample-app")
    assert (root / KEY).read_bytes() == filled


def test_a_filled_in_context_survives_a_template_change(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    filled = _fill_identity(root / KEY)
    real = pack_render.render

    def newer(*args, **kwargs):
        mapping = dict(real(*args, **kwargs))
        mapping[KEY] = mapping[KEY] + b"\nA newer template line.\n"
        return mapping

    monkeypatch.setattr(pack_render, "render", newer)
    _resync(folder)
    assert (root / KEY).read_bytes() == filled


def test_an_untouched_context_still_takes_a_newer_template(
        tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    real = pack_render.render

    def newer(*args, **kwargs):
        mapping = dict(real(*args, **kwargs))
        mapping[KEY] = mapping[KEY] + b"\nA newer template line.\n"
        return mapping

    monkeypatch.setattr(pack_render, "render", newer)
    _resync(folder)
    text = (root / KEY).read_bytes()
    assert b"A newer template line." in text
    assert pack_ledger.digests_of(pack_ledger.entry(folder))[KEY] == (
        pack_render.managed_digest(text))
    # ...and the refreshed region is still the pack's, so the next one lands.
    monkeypatch.setattr(pack_render, "render", real)
    _resync(folder)
    assert b"A newer template line." not in (root / KEY).read_bytes()


def test_a_row_from_an_older_build_keeps_an_edited_context(
        tmp_path, monkeypatch):
    # vir-sunset's case: the ledger row predates `pack_digests`.
    root, folder = _project(tmp_path, monkeypatch)
    filled = _fill_identity(root / KEY)
    pack_ledger.update(folder, pack_digests={})
    _resync(folder)
    assert (root / KEY).read_bytes() == filled


def test_a_context_without_markers_keeps_its_text_below(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    own = b"# Our own notes\n\nkeep this paragraph\n"
    (root / KEY).write_bytes(own)
    _resync(folder)
    text = (root / KEY).read_bytes()
    assert text.startswith(pack_render.BEGIN_MARK.encode())
    assert text.endswith(own)
    # Once spliced it is the pack's region again, until somebody edits it.
    _resync(folder)
    assert (root / KEY).read_bytes() == text


def test_text_outside_the_markers_is_kept_on_a_refresh(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    path = root / KEY
    path.write_bytes(path.read_bytes() + b"\n## Our appendix\n")
    _resync(folder)
    assert path.read_bytes().endswith(b"## Our appendix\n")


def test_a_deleted_context_is_seeded_again(tmp_path, monkeypatch):
    root, folder = _project(tmp_path, monkeypatch)
    _fill_identity(root / KEY)
    (root / KEY).unlink()
    _resync(folder)
    assert b"`pnpm lint`" in (root / KEY).read_bytes()


def test_crlf_checkout_reads_as_unedited():
    region = pack_render.splice(b"", b"body\n")
    assert pack_render.managed_digest(region) == pack_render.managed_digest(
        region.replace(b"\n", b"\r\n"))
    assert pack_render.managed_digest(b"no markers here\n") == ""


def test_ledger_row_without_digests_reads_empty():
    assert pack_ledger.digests_of({"root": "/x"}) == {}
    assert pack_ledger.digests_of({"pack_digests": "junk"}) == {}
    assert pack_ledger.digests_of(None) == {}
