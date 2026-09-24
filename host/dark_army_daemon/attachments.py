"""Bounds and checks for files attached to a board card.

Copies live under ``ATTACHMENTS_DIR`` as ``<folder>/<sanitised-name>``. The
store holds that relative path as a newline-separated string and stays pure:
shape, count and extension are refused here as sentences, and the filesystem
is consulted only at the moment of use (Prepare, dispatch, delete). A name
that is a line of instructions or a path walk is defanged before it is stored
or quoted in a prompt — the sanitised form is the only form.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from .paths import ATTACHMENTS_DIR


MAX_ATTACHMENTS_PER_CARD = 8
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_NAME_CHARS = 80
ALLOWED_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "heic",
    "pdf", "txt", "md", "markdown", "log", "json", "csv", "rtf",
})
ORPHAN_SWEEP_SECONDS = 24 * 3600

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_UNDERSCORES = re.compile(r"_+")
_FOLDER = re.compile(r"^[a-z0-9-]{8,40}$")


def split_field(value) -> list[str]:
    """A stored attachments column back into relative paths."""
    return [line.strip() for line in str(value or "").split("\n") if line.strip()]


def sanitize_name(name) -> Optional[str]:
    """The stored filename, or None if nothing safe remains.

    Basename, NFC, charset ``[A-Za-z0-9._-]``, collapsed underscores, leading
    dots stripped (so ``..`` and hidden files die in one move), then capped at
    ``MAX_NAME_CHARS`` keeping the extension.
    """
    base = unicodedata.normalize("NFC", os.path.basename(str(name or "")))
    base = _UNDERSCORES.sub("_", _UNSAFE.sub("_", base)).lstrip(".")
    if not base:
        return None
    if len(base) > MAX_NAME_CHARS:
        stem, ext = os.path.splitext(base)
        if len(ext) >= MAX_NAME_CHARS:
            base = ext[-MAX_NAME_CHARS:].lstrip(".")
        else:
            base = (stem[: MAX_NAME_CHARS - len(ext)] + ext).lstrip(".")
        if not base:
            return None
    return base


def field_refusal(value) -> Optional[str]:
    """Why a stored attachments value is not acceptable, or None.

    Pure: no filesystem. Each line must be ``<folder>/<name>`` whose folder
    matches ``[a-z0-9-]{8,40}`` and whose name is ``sanitize_name``'s fixed
    point with an allowed extension. Duplicates and a ninth file are refused
    with a sentence, never trimmed.
    """
    lines = split_field(value)
    if not lines:
        return None
    if len(lines) > MAX_ATTACHMENTS_PER_CARD:
        return (f"a card can have at most {MAX_ATTACHMENTS_PER_CARD} "
                "attachments")
    seen: set[str] = set()
    for line in lines:
        # A walk is already impossible once the line is exactly one slash,
        # the folder matches ``[a-z0-9-]{8,40}``, and the name is
        # ``sanitize_name``'s fixed point. A substring ``..`` would refuse
        # ``photo..png``.
        if line.startswith("/") or line.count("/") != 1:
            return "that is not a stored attachment path"
        folder, name = line.split("/", 1)
        if not _FOLDER.fullmatch(folder):
            return "that is not a stored attachment path"
        if sanitize_name(name) != name:
            return "that is not a stored attachment path"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in ALLOWED_EXTENSIONS:
            if ext:
                return f".{ext} files are not allowed"
            return "that file type is not allowed"
        if line in seen:
            return "the same file is attached twice"
        seen.add(line)
    return None


def folder_name_ok(name) -> bool:
    """Whether ``name`` is a legal staging-folder name.

    The public face of ``_FOLDER``. Named rather than exported as the regex so
    an upload route re-checks the shape by asking this module, not by growing
    a second copy of ``[a-z0-9-]{8,40}``.
    """
    return bool(_FOLDER.fullmatch(str(name or "")))


def store_upload(staging_id, name, data) -> tuple[Optional[str], Optional[str]]:
    """Write one uploaded file into its staging folder. Blocking.

    Every bound is re-checked here even though the sender pre-checks: the
    folder shape, ``sanitize_name``'s fixed point, the extension, the byte
    count, and the per-card ceiling counted from what is already in the
    folder. A name collision is deduped with a numeric suffix the way the
    desktop composer's copy does, so a second photo of the same name never
    clobbers the first.

    Returns ``("<folder>/<stored-name>", None)`` — the *stored* path, which
    is what the caller must record — or ``(None, sentence)``.
    """
    folder = str(staging_id or "")
    if not folder_name_ok(folder):
        return None, "that is not a stored attachment path"
    safe = sanitize_name(name)
    if not safe or safe != str(name or ""):
        return None, "that is not a stored attachment path"
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    if ext not in ALLOWED_EXTENSIONS:
        if ext:
            return None, f".{ext} files are not allowed"
        return None, "that file type is not allowed"
    blob = bytes(data or b"")
    if len(blob) > MAX_ATTACHMENT_BYTES:
        mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return None, f"that file is larger than {mb} MB"

    directory = ATTACHMENTS_DIR / folder
    try:
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None, "Dark Army could not write that file"
    try:
        existing = sorted(entry.name for entry in directory.iterdir()
                          if entry.is_file())
    except OSError:
        return None, "Dark Army could not write that file"
    if len(existing) >= MAX_ATTACHMENTS_PER_CARD:
        return (None, f"a card can have at most {MAX_ATTACHMENTS_PER_CARD} "
                      "attachments")

    stem, dot_ext = os.path.splitext(safe)
    candidate = safe
    # Bounded: the folder can hold at most `MAX_ATTACHMENTS_PER_CARD` files,
    # so a free name is always found inside the ceiling plus one.
    for attempt in range(2, MAX_ATTACHMENTS_PER_CARD + 3):
        if candidate not in existing:
            break
        probe = sanitize_name(f"{stem}-{attempt}{dot_ext}")
        if not probe:
            return None, "that is not a stored attachment path"
        candidate = probe
    else:
        return None, "that file is already attached"
    if candidate in existing:
        return None, "that file is already attached"

    target = directory / candidate
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None, "that file is already attached"
    except OSError:
        return None, "Dark Army could not write that file"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        return None, "Dark Army could not write that file"
    return f"{folder}/{candidate}", None


def _contained(path: Path, root: Path) -> bool:
    """True when ``path`` (already resolved) sits inside ``root``."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def read_refusal(rel_paths) -> tuple[Optional[list[str]], Optional[str]]:
    """Filesystem check at the moment of use.

    Each ``ATTACHMENTS_DIR / rel`` must realpath-resolve inside
    ``ATTACHMENTS_DIR``, be a regular file, and be ≤ ``MAX_ATTACHMENT_BYTES``.
    Returns ``(abs_paths, None)`` or ``(None, sentence)``.
    """
    try:
        root = ATTACHMENTS_DIR.resolve()
    except OSError:
        return None, "Dark Army's attachments folder is not there"
    abs_paths: list[str] = []
    for rel in split_field("\n".join(str(p) for p in (rel_paths or ()))):
        candidate = ATTACHMENTS_DIR / rel
        try:
            resolved = candidate.resolve()
        except OSError:
            return None, "that attached file is gone"
        if not _contained(resolved, root):
            return None, "that file is not inside Dark Army's attachments folder"
        if not resolved.is_file() or resolved.is_symlink():
            # ``is_file`` follows a symlink; a link that resolved inside and
            # still *is* a symlink is a directory entry we refuse rather than
            # opening, because the copy on disk is supposed to be a regular
            # file the panel wrote.
            if not resolved.is_file():
                return None, "that attached file is gone"
            return None, "that file is not inside Dark Army's attachments folder"
        try:
            size = resolved.stat().st_size
        except OSError:
            return None, "that attached file is gone"
        if size > MAX_ATTACHMENT_BYTES:
            mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
            return None, f"that file is larger than {mb} MB"
        abs_paths.append(str(resolved))
    return abs_paths, None


#: The one sentence that heads the attachment block. Named, because the
#: strip below has to recognise a line the block itself may have written —
#: two literals of the same sentence would drift.
ATTACHMENT_HEADER = "Attached files (open with your file tools):"

_PROMPT_BLOCK_HEAD = "\n\n" + ATTACHMENT_HEADER + "\n"

_LIST_MARKER = re.compile(r"^[-*]\s+")
_BLANK_RUN = re.compile(r"\n{3,}")


def prompt_block_from_abs(abs_paths) -> str:
    """The spawn/Prepare suffix from already-resolved absolute paths, or ``""``."""
    lines = [str(p).strip() for p in (abs_paths or ()) if str(p).strip()]
    if not lines:
        return ""
    return _PROMPT_BLOCK_HEAD + "\n".join(lines)


def strip_path_lines(text, abs_paths) -> str:
    """``text`` with every line that is *only* an attachment link removed.

    Whole-line equality after one bounded defang — an optional leading ``- ``
    or ``* `` marker and one layer of surrounding backticks — against
    ``ATTACHMENT_HEADER`` or any entry of ``abs_paths``. A path named
    mid-sentence survives: prose about a file is not a listing of it, and
    anything cleverer here silently edits somebody's instructions.

    Runs of three or more newlines left behind collapse to two, and the
    result is rstripped. Empty ``abs_paths`` returns ``text`` unchanged.
    """
    body = text or ""
    wanted = {str(p).strip() for p in (abs_paths or ()) if str(p).strip()}
    if not wanted:
        return body
    kept: list[str] = []
    for line in body.split("\n"):
        probe = _LIST_MARKER.sub("", line.strip(), count=1).strip()
        if len(probe) >= 2 and probe[0] == "`" and probe[-1] == "`":
            probe = probe[1:-1].strip()
        if probe == ATTACHMENT_HEADER or probe in wanted:
            continue
        kept.append(line)
    return _BLANK_RUN.sub("\n\n", "\n".join(kept)).rstrip()


def resolve_paths(rel_paths) -> list[str]:
    """The surviving absolute copies of a card's stored attachment paths.

    A vanished file is omitted rather than refusing the work; nothing
    surviving is an empty list, so a card with only missing copies starts
    like one with none. Factored out of `prompt_block` so a dispatch site
    can get the paths *and* the block from one resolution — the strip needs
    the paths, the prompt needs the block.
    """
    try:
        root = ATTACHMENTS_DIR.resolve()
    except OSError:
        return []
    lines: list[str] = []
    for rel in split_field("\n".join(str(p) for p in (rel_paths or ()))):
        try:
            resolved = (ATTACHMENTS_DIR / rel).resolve()
        except OSError:
            continue
        if not _contained(resolved, root) or not resolved.is_file():
            continue
        lines.append(str(resolved))
    return lines


def prompt_block(rel_paths) -> str:
    """Text appended to a dispatched card's prompt, or ``""``."""
    return prompt_block_from_abs(resolve_paths(rel_paths))


def folders_of(value) -> set[str]:
    """Staging-folder names referenced by a stored attachments field."""
    out: set[str] = set()
    for rel in split_field(value):
        folder = rel.split("/", 1)[0]
        if folder:
            out.add(folder)
    return out


def referenced_folders(cards: Iterable) -> set[str]:
    """Union of staging folders still named by any card."""
    out: set[str] = set()
    for card in cards or ():
        if isinstance(card, dict):
            out |= folders_of(card.get("attachments"))
    return out


def remove_unreferenced(candidates: Iterable[str], keep: Iterable[str]) -> None:
    """Delete staging folders in ``candidates`` that no surviving card names."""
    keep_set = set(keep)
    root = ATTACHMENTS_DIR
    for name in set(candidates) - keep_set:
        if not _FOLDER.fullmatch(str(name)):
            continue
        path = root / name
        try:
            resolved = path.resolve()
            root_resolved = root.resolve()
        except OSError:
            continue
        if not _contained(resolved, root_resolved) or resolved == root_resolved:
            continue
        shutil.rmtree(resolved, ignore_errors=True)


def sweep_orphans(keep: Iterable[str], now: Optional[float] = None) -> int:
    """Remove unreferenced staging folders older than ``ORPHAN_SWEEP_SECONDS``.

    The age guard is what protects a composer that is open right now with
    staged files: those folders are referenced by no card yet, and must not
    be deleted from under the sheet.
    """
    if not ATTACHMENTS_DIR.is_dir():
        return 0
    try:
        root = ATTACHMENTS_DIR.resolve()
    except OSError:
        return 0
    keep_set = set(keep)
    stamp = time.time() if now is None else now
    removed = 0
    try:
        entries = list(ATTACHMENTS_DIR.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.name in keep_set:
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if not _contained(resolved, root) or resolved == root:
            continue
        if not resolved.is_dir():
            continue
        try:
            mtime = resolved.stat().st_mtime
        except OSError:
            continue
        if stamp - mtime < ORPHAN_SWEEP_SECONDS:
            continue
        shutil.rmtree(resolved, ignore_errors=True)
        removed += 1
    return removed


__all__ = [
    "MAX_ATTACHMENTS_PER_CARD", "MAX_ATTACHMENT_BYTES", "MAX_NAME_CHARS",
    "ALLOWED_EXTENSIONS", "ORPHAN_SWEEP_SECONDS",
    "ATTACHMENT_HEADER",
    "sanitize_name", "field_refusal", "read_refusal",
    "folder_name_ok", "store_upload",
    "prompt_block", "prompt_block_from_abs", "resolve_paths",
    "strip_path_lines",
    "split_field", "folders_of", "referenced_folders",
    "remove_unreferenced", "sweep_orphans",
]
