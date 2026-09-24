"""Bounds, sanitisation, containment and the Swift↔Python contract."""
import stat
from pathlib import Path

import pytest

from dark_army_daemon import attachments


FOLDER = "abcd1234-efgh5678-ijkl9012-mnop34"


def test_sanitize_strips_a_path_walk_to_the_basename():
    assert attachments.sanitize_name("../../etc/passwd") == "passwd"


def test_sanitize_caps_a_long_name_keeping_the_extension():
    name = "x" * 200 + ".png"
    out = attachments.sanitize_name(name)
    assert out.endswith(".png")
    assert len(out) == attachments.MAX_NAME_CHARS


def test_sanitize_reduces_spaces_and_quotes_to_the_charset():
    out = attachments.sanitize_name("  'foo bar'.txt")
    assert out is not None
    assert all(c.isalnum() or c in "._-" for c in out)
    assert " " not in out
    assert "'" not in out
    assert out.endswith(".txt")


def test_sanitize_strips_leading_dots():
    assert attachments.sanitize_name(".hidden.png") == "hidden.png"
    assert attachments.sanitize_name("..secret.txt") == "secret.txt"


def test_sanitize_refuses_an_empty_name():
    assert attachments.sanitize_name("...") is None


def test_field_refusal_accepts_empty_and_a_valid_row():
    assert attachments.field_refusal("") is None
    assert attachments.field_refusal(f"{FOLDER}/shot.png") is None


def test_field_refusal_accepts_a_name_with_double_dot_in_the_stem():
    assert attachments.field_refusal(f"{FOLDER}/photo..png") is None
    assert attachments.field_refusal(f"{FOLDER}/v1..2.pdf") is None


def test_field_refusal_refuses_a_ninth_entry():
    lines = [f"{FOLDER}/f{i}.png" for i in range(9)]
    reason = attachments.field_refusal("\n".join(lines))
    assert reason
    assert "8" in reason


def test_field_refusal_refuses_a_shell_script():
    reason = attachments.field_refusal(f"{FOLDER}/run.sh")
    assert reason
    assert ".sh" in reason


def test_field_refusal_refuses_a_parent_walk():
    reason = attachments.field_refusal("../etc/passwd")
    assert reason
    assert "path" in reason


def test_field_refusal_refuses_an_absolute_path():
    reason = attachments.field_refusal("/tmp/shot.png")
    assert reason
    assert "path" in reason


def test_field_refusal_refuses_a_duplicate():
    rel = f"{FOLDER}/shot.png"
    reason = attachments.field_refusal(f"{rel}\n{rel}")
    assert reason
    assert "twice" in reason


def _patch_dir(monkeypatch, tmp_path):
    folder = tmp_path / "attachments"
    folder.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    return folder


def test_read_refusal_refuses_a_symlink_escaping_the_dir(tmp_path, monkeypatch):
    root = _patch_dir(monkeypatch, tmp_path)
    staging = root / FOLDER
    staging.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"no")
    link = staging / "shot.png"
    link.symlink_to(secret)
    paths, reason = attachments.read_refusal([f"{FOLDER}/shot.png"])
    assert paths is None
    assert reason
    assert "inside" in reason


def test_read_refusal_refuses_a_missing_file(tmp_path, monkeypatch):
    _patch_dir(monkeypatch, tmp_path)
    paths, reason = attachments.read_refusal([f"{FOLDER}/gone.png"])
    assert paths is None
    assert reason
    assert "gone" in reason


def test_read_refusal_refuses_an_oversize_file(tmp_path, monkeypatch):
    root = _patch_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 10)
    staging = root / FOLDER
    staging.mkdir()
    (staging / "shot.png").write_bytes(b"x" * 20)
    paths, reason = attachments.read_refusal([f"{FOLDER}/shot.png"])
    assert paths is None
    assert reason
    assert "larger" in reason


def test_read_refusal_passes_a_real_file(tmp_path, monkeypatch):
    root = _patch_dir(monkeypatch, tmp_path)
    staging = root / FOLDER
    staging.mkdir()
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    paths, reason = attachments.read_refusal([f"{FOLDER}/shot.png"])
    assert reason is None
    assert paths == [str(dest.resolve())]


def test_prompt_block_omits_a_vanished_file_and_is_empty_on_none(
        tmp_path, monkeypatch):
    root = _patch_dir(monkeypatch, tmp_path)
    staging = root / FOLDER
    staging.mkdir()
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    block = attachments.prompt_block(
        [f"{FOLDER}/shot.png", f"{FOLDER}/gone.pdf"])
    assert "Attached files (open with your file tools):" in block
    assert str(dest.resolve()) in block
    assert "gone.pdf" not in block
    assert attachments.prompt_block([]) == ""
    assert attachments.prompt_block([f"{FOLDER}/gone.pdf"]) == ""
    assert attachments.prompt_block_from_abs([]) == ""
    from_abs = attachments.prompt_block_from_abs([str(dest.resolve())])
    assert from_abs == block


def test_resolve_paths_omits_a_vanished_file_and_is_empty_on_none(
        tmp_path, monkeypatch):
    root = _patch_dir(monkeypatch, tmp_path)
    staging = root / FOLDER
    staging.mkdir()
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    got = attachments.resolve_paths(
        [f"{FOLDER}/shot.png", f"{FOLDER}/gone.pdf"])
    assert got == [str(dest.resolve())]
    assert attachments.resolve_paths([]) == []
    assert attachments.resolve_paths([f"{FOLDER}/gone.pdf"]) == []


def test_strip_path_lines_drops_only_whole_lines():
    """A listing goes; a sentence that happens to name the file stays. The
    strip must never silently edit somebody's prose."""
    shot = "/tmp/att/shot.png"
    other = "/tmp/att/notes.md"
    text = (
        "Fix the wrap.\n"
        f"See {shot} for the layout.\n\n"
        f"{attachments.ATTACHMENT_HEADER}\n"
        f"{shot}\n"
        f"- {other}\n"
        f"  `{shot}`  \n"
    )
    got = attachments.strip_path_lines(text, [shot, other])
    assert got == f"Fix the wrap.\nSee {shot} for the layout."
    assert attachments.ATTACHMENT_HEADER not in got


def test_strip_path_lines_collapses_the_gap_it_leaves():
    shot = "/tmp/att/shot.png"
    text = f"One.\n\n{shot}\n\nTwo.\n"
    assert attachments.strip_path_lines(text, [shot]) == "One.\n\nTwo."


def test_strip_path_lines_with_no_paths_is_a_no_op():
    text = f"Keep me.\n{attachments.ATTACHMENT_HEADER}\n\n\n"
    assert attachments.strip_path_lines(text, []) == text
    assert attachments.strip_path_lines(text, None) == text
    assert attachments.strip_path_lines("", ["/tmp/a.png"]) == ""


def test_swift_constants_match_the_python_bounds():
    """The panel and the store must refuse the same files."""
    repo = Path(__file__).resolve().parents[2]
    src = (repo / "panel/Sources/BobPanel/Attachments.swift").read_text(
        encoding="utf-8")
    assert f"static let maxPerCard = {attachments.MAX_ATTACHMENTS_PER_CARD}" in src
    assert "static let maxBytes = 20 * 1024 * 1024" in src
    assert attachments.MAX_ATTACHMENT_BYTES == 20 * 1024 * 1024
    assert f"static let maxNameChars = {attachments.MAX_NAME_CHARS}" in src
    for ext in attachments.ALLOWED_EXTENSIONS:
        assert f'"{ext}"' in src
    listed = set()
    start = src.index("allowedExtensions")
    block = src[start:src.index("]", start)]
    for ext in attachments.ALLOWED_EXTENSIONS:
        if f'"{ext}"' in block:
            listed.add(ext)
    assert listed == attachments.ALLOWED_EXTENSIONS


# --- store_upload -------------------------------------------------------------


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """`ATTACHMENTS_DIR` is bound at import, so patch it on the module."""
    root = tmp_path / "attachments"
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", root)
    return root


def test_folder_name_ok_is_the_public_face_of_the_folder_shape():
    assert attachments.folder_name_ok(FOLDER) is True
    assert attachments.folder_name_ok("short") is False
    assert attachments.folder_name_ok("UPPER1234") is False
    assert attachments.folder_name_ok("") is False
    assert attachments.folder_name_ok(None) is False


def test_store_upload_writes_0600_and_returns_the_stored_path(upload_dir):
    rel, detail = attachments.store_upload(FOLDER, "shot.png", b"data")
    assert detail is None
    assert rel == f"{FOLDER}/shot.png"
    written = upload_dir / FOLDER / "shot.png"
    assert written.read_bytes() == b"data"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    # The stored shape the board will re-validate at create.
    assert attachments.field_refusal(rel) is None


def test_store_upload_refuses_a_name_that_is_not_its_own_fixed_point(upload_dir):
    rel, detail = attachments.store_upload(FOLDER, "../escape.png", b"x")
    assert rel is None
    assert detail == "that is not a stored attachment path"
    assert not (upload_dir / FOLDER).exists()


def test_store_upload_refuses_a_folder_that_is_the_attachments_root(upload_dir):
    rel, detail = attachments.store_upload(".", "shot.png", b"x")
    assert rel is None
    assert detail == "that is not a stored attachment path"


def test_store_upload_refuses_a_disallowed_extension(upload_dir):
    rel, detail = attachments.store_upload(FOLDER, "run.sh", b"echo")
    assert rel is None
    assert detail == ".sh files are not allowed"


def test_store_upload_refuses_an_oversize_file(upload_dir, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 4)
    rel, detail = attachments.store_upload(FOLDER, "shot.png", b"toolong")
    assert rel is None
    assert "larger than" in detail


def test_store_upload_dedupes_a_collision_rather_than_clobbering(upload_dir):
    first, _ = attachments.store_upload(FOLDER, "shot.png", b"one")
    second, detail = attachments.store_upload(FOLDER, "shot.png", b"two")
    assert detail is None
    assert first == f"{FOLDER}/shot.png"
    assert second == f"{FOLDER}/shot-2.png"
    assert (upload_dir / FOLDER / "shot.png").read_bytes() == b"one"
    assert (upload_dir / FOLDER / "shot-2.png").read_bytes() == b"two"


def test_store_upload_refuses_a_ninth_file(upload_dir):
    for index in range(attachments.MAX_ATTACHMENTS_PER_CARD):
        rel, detail = attachments.store_upload(FOLDER, f"s{index}.png", b"x")
        assert detail is None, detail
    rel, detail = attachments.store_upload(FOLDER, "ninth.png", b"x")
    assert rel is None
    assert detail == "a card can have at most 8 attachments"
