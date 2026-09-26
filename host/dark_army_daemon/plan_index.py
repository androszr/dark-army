"""Every written plan Dark Army knows about, as one list, and one plan's
text.

`scout_index.py`'s twin for plans. **Pure over paths and dicts.** The only
I/O is `os.scandir`, `os.lstat` / `os.stat`, one bounded head read per
listed plan (`scout_index._read_head`) and `scout_report.read_text` for a
body — all **blocking**, so the daemon runs `build` and `read` inside one
`run_in_executor` hop each (`BoardVerbsMixin.plans_index` / `plan_body`).

**Where a plan comes from — two facts and nothing else.** The dated files
`<root>/plans/<YYYY-MM-DD>-<slug>.md` under every enrolled root, and the
cards' `plan_path` column (`attach_plan`'s ring), which may name a plan
outside `plans/` (`dispatch.named_plan` accepts any `.md` inside the root).
A card whose plan the scan already reached *annotates* that row; it never
duplicates it. `FILE_RE` alone decides what is a plan in the folder: the
folder's `README.md`, its question list and any directory are left out by
the dated-name rule, never by a special case.

**The closed set is the body read's security boundary** (`locate`): a
client may send any string as `path`; only a card's stored `plan_path`
(contained in that card's enrolled root) or `<enrolled root>/plans/<dated
name>.md` (exactly two components under that root) is ever read, both
through the attach's own `_plan_path_refusal` realpath rule — never "any
`.md` under an enrolled root".

**Bounds.** At most `MAX_ROOTS` roots and `MAX_FILES_PER_ROOT` dated files
per root (the newest names win); heads of `INDEX_HEAD_BYTES`; the list
stops reading heads once it plainly exceeds `PAGE_BYTES` and the API trims
exactly, dropping the oldest; a body is `scout_report.MAX_REPORT_BYTES`
(64 KiB, the same number as `board_workflow.MAX_PLAN_BYTES`) and a plan
over it is `UNREADABLE` in words, never clipped.

The shared helpers are imported from `scout_index`, not copied, so a fix
to the head read lands on both lists.
"""

import os
import re
import stat

from . import board_workflow
from . import scout_report
from .scout_index import (
    INDEX_HEAD_BYTES,
    MAX_ROOTS,
    PAGE_BYTES,
    _inside,
    _normalise,
    _read_head,
    _refusal_default,
    _title,
)

__all__ = ["FOLDER", "FILE_RE", "MAX_FILES_PER_ROOT", "INDEX_HEAD_BYTES",
           "MAX_ROOTS", "PAGE_BYTES", "NOT_LISTED", "UNREADABLE",
           "scan_root", "row", "build", "locate", "read"]

#: The folder under an enrolled root that holds the dated plans.
FOLDER = "plans"
#: A dated plan file: `YYYY-MM-DD-<slug>.md`. Group 1 is the day, group 2
#: the slug.
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([^/\n]+)\.md\Z")
#: At most this many dated files per root, the newest by name.
MAX_FILES_PER_ROOT = 600

#: The body read's refusal for anything outside the closed set. The plan's
#: own words rather than `scout_index`'s, which say "report" — the phone
#: draws the sentence on the plan's screen.
NOT_LISTED = "that plan is not one Dark Army lists"
#: The body read's words when a listed file could not be read (gone, not a
#: regular file, or over the 64 KiB bound).
UNREADABLE = "that plan could not be read"

_STATUS_LINE = re.compile(r"^(?:-\s*)?\*\*Status:\*\*\s*(.*)$")
_AREA_LINE = re.compile(r"^(?:-\s*)?\*\*Area:\*\*\s*(.*)$")
_H1 = re.compile(r"^# \S")
#: A header value is drawn on one row; longer is not a header value.
_HEADER_VALUE_MAX = 200
#: A rough per-row JSON size for `build`'s "plainly exceeded" stop.
_ROW_OVERHEAD = 400


def _header_value(text: str, pattern) -> str:
    """The first `- **Label:** value` line above the first `## ` section,
    `board_workflow._header_lines`' rule, with backticks trimmed."""
    for line in board_workflow._header_lines(text or ""):
        match = pattern.match(line.strip())
        if match:
            value = match.group(1).strip().strip("`").strip()
            return value[:_HEADER_VALUE_MAX]
    return ""


def _name_parts(path: str) -> tuple:
    """``(name, day, slug)`` from the file name; ``day`` is ``""`` for a
    name without a date (a card's plan outside `plans/`)."""
    name = os.path.basename(path)
    match = FILE_RE.match(name)
    if match:
        return name, match.group(1), match.group(2)
    slug = name[:-3] if name.lower().endswith(".md") else name
    return name, "", slug


def scan_root(root: str) -> list:
    """The dated plan files under `<root>/plans/`, newest name first, at
    most `MAX_FILES_PER_ROOT`. Each entry is ``{"path", "name", "day",
    "slug", "stat"}`` — ``path`` the realpath of a regular file (no
    symlink, final or folder) sitting exactly at `<root>/plans/<name>`.
    An undated name, a directory, a symlink, a FIFO or a file resolving
    anywhere else is skipped."""
    base = _normalise(root)
    if not base:
        return []
    folder_dir = os.path.join(base, FOLDER)
    try:
        with os.scandir(folder_dir) as listing:
            names = [e.name for e in listing
                     if FILE_RE.match(e.name)
                     and e.is_file(follow_symlinks=False)]
    except OSError:
        return []
    names.sort(reverse=True)
    found = []
    for name in names[:MAX_FILES_PER_ROOT]:
        candidate = os.path.join(folder_dir, name)
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        resolved = os.path.realpath(candidate)
        if not _inside(base, resolved) or resolved != candidate:
            continue
        match = FILE_RE.match(name)
        found.append({"path": resolved, "name": name,
                      "day": match.group(1), "slug": match.group(2),
                      "stat": st})
    return found


def row(path: str, root: str, card, st, head_text: str, *,
        project: str = "") -> dict:
    """One list row. **No `body`**: the body is the second, separate
    read."""
    card = card or {}
    name, day, slug = _name_parts(path)
    return {
        "path": path,
        "root": root,
        "project": project or os.path.basename(root),
        "name": name,
        "slug": slug,
        "day": day,
        "written_at": float(getattr(st, "st_mtime", 0.0) or 0.0),
        "title": _title(head_text),
        "status": _header_value(head_text, _STATUS_LINE),
        "area": _header_value(head_text, _AREA_LINE),
        "card_id": str(card.get("id") or ""),
        "card_title": str(card.get("title") or ""),
        "card_column": str(card.get("column_name") or ""),
        "bytes": int(getattr(st, "st_size", 0) or 0),
    }


def _card_root(card: dict) -> str:
    return _normalise(str(card.get("root") or ""))


def _order(candidates) -> list:
    """Newest first by the filename's day (an undated plan last), then
    `written_at` descending, then `path` ascending. The day leads so a plan
    iterated today keeps its place."""
    ordered = sorted(candidates, key=lambda c: c["path"])
    ordered.sort(key=lambda c: (c["day"], float(c["stat"].st_mtime)),
                 reverse=True)
    return ordered


def build(roots, cards, label_for=None, *, refusal=None) -> dict:
    """The whole list, newest first (`_order`).

    ``roots`` are enrolled roots; ``cards`` any iterable of card dicts
    (``id``, ``title``, ``column_name``, ``root``, ``plan_path``) in
    `CARD_ORDER_SQL`; ``label_for(root)`` names a project. A card's
    `plan_path` the scan did not reach is its own row after
    `refusal(card root, path)` admits it, and only for a card whose root is
    one of ``roots``; the first card on a plan annotates its row. Heads are
    read newest first and the reading stops once the rows plainly exceed
    `PAGE_BYTES`; ``omitted`` counts the rest."""
    refusal = refusal or _refusal_default()
    label_for = label_for or (lambda r: os.path.basename(r))
    wanted = []
    for root in roots or ():
        base = _normalise(str(root or ""))
        if base and base not in wanted:
            wanted.append(base)
    wanted = sorted(wanted)[:MAX_ROOTS]
    members = set(wanted)
    candidates = {}
    for base in wanted:
        for entry in scan_root(base):
            candidates.setdefault(entry["path"], {
                "path": entry["path"], "root": base, "stat": entry["stat"],
                "day": entry["day"], "card": None})
    for card in cards or ():
        stored = str(card.get("plan_path") or "").strip()
        if not stored:
            continue
        base = _card_root(card)
        if base not in members:
            continue
        resolved, why = refusal(base, stored)
        if why or not resolved:
            continue
        existing = candidates.get(resolved)
        if existing is not None:
            if existing["card"] is None:
                existing["card"] = card
            continue
        try:
            st = os.lstat(resolved)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        _name, day, _slug = _name_parts(resolved)
        candidates[resolved] = {"path": resolved, "root": base,
                                "stat": st, "day": day, "card": card}
    ordered = _order(candidates.values())
    rows = []
    spent = 0
    omitted = 0
    labels = {}
    for index, candidate in enumerate(ordered):
        if spent > PAGE_BYTES:
            omitted = len(ordered) - index
            break
        base = candidate["root"]
        if base not in labels:
            labels[base] = str(label_for(base) or os.path.basename(base))
        head, _whole = _read_head(candidate["path"],
                                  int(candidate["stat"].st_size))
        item = row(candidate["path"], base, candidate["card"],
                   candidate["stat"], head, project=labels[base])
        spent += _ROW_OVERHEAD + sum(
            len(v) for v in item.values() if isinstance(v, str))
        rows.append(item)
    return {"supported": True, "available": True, "rows": rows,
            "truncated": omitted > 0, "omitted": omitted,
            "roots": len(wanted)}


def locate(path: str, roots, cards, *, refusal=None) -> tuple:
    """``(resolved, root, refusal_words)`` for a body read — the closed set
    re-checked **at the moment of the read**, never trusting the row the
    client held. Admitted: a card's stored `plan_path` (its root one of
    ``roots``, contained by that root), or `<root>/plans/<dated name>.md`
    under one of ``roots`` — exactly two components under the root after
    realpath, so a symlinked file or folder that resolves anywhere else is
    refused. Anything else is `NOT_LISTED`."""
    refusal = refusal or _refusal_default()
    text = str(path or "").strip()
    if not text or len(text) > 4096 or not os.path.isabs(text):
        return "", "", NOT_LISTED
    members = {b for b in (_normalise(str(r or "")) for r in roots or ()) if b}
    target = os.path.realpath(text)
    for card in cards or ():
        stored = str(card.get("plan_path") or "").strip()
        if not stored:
            continue
        base = _card_root(card)
        if base not in members:
            continue
        candidate = stored if os.path.isabs(os.path.expanduser(stored)) \
            else os.path.join(base, stored)
        if os.path.realpath(os.path.expanduser(candidate)) != target:
            continue
        resolved, why = refusal(base, stored)
        if resolved and not why and resolved == target:
            return resolved, base, ""
    for base in sorted(members):
        if not _inside(base, target):
            continue
        parts = target[len(base) + len(os.sep):].split(os.sep)
        if (len(parts) == 2
                and parts[0].casefold() == FOLDER.casefold()
                and FILE_RE.match(parts[1])):
            resolved, why = refusal(base, target)
            if resolved and not why:
                return resolved, base, ""
    return "", "", NOT_LISTED


def _strip_h1(text: str) -> str:
    """The text with its first H1 line removed (the client draws the
    title), leading blank lines trimmed; a text without an H1 is whole."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _H1.match(line):
            return "".join(lines[:index] + lines[index + 1:]).lstrip("\r\n")
    return text


def read(path: str, roots, cards, label_for=None, *, refusal=None) -> dict:
    """One plan: ``{"available", "path", "root", "project", "name", "day",
    "title", "status", "area", "body", "written_at", "reason"}``. `locate`
    first; `scout_report.read_text` on the resolved path (whole file,
    bounded, no final symlink, regular file only). ``body`` is the text
    without its first H1 line."""
    label_for = label_for or (lambda r: os.path.basename(r))
    resolved, root, why = locate(path, roots, cards, refusal=refusal)
    answer = {"available": False, "path": str(path or ""), "root": root,
              "project": "", "name": "", "day": "", "title": "",
              "status": "", "area": "", "body": "", "written_at": 0.0,
              "reason": why}
    if why:
        return answer
    name, day, _slug = _name_parts(resolved)
    answer.update({
        "path": resolved,
        "project": str(label_for(root) or os.path.basename(root)),
        "name": name,
        "day": day,
    })
    text = scout_report.read_text(resolved)
    if not text:
        answer["reason"] = UNREADABLE
        return answer
    try:
        written_at = float(os.stat(resolved).st_mtime)
    except OSError:
        written_at = 0.0
    answer.update({
        "available": True,
        "title": _title(text),
        "status": _header_value(text, _STATUS_LINE),
        "area": _header_value(text, _AREA_LINE),
        "body": _strip_h1(text),
        "written_at": written_at,
        "reason": "",
    })
    return answer
