"""Every scout report Dark Army knows about, as one list, and one report's
text split into its answer block and its body.

**Pure over paths and dicts.** The only I/O is `os.scandir`, `os.lstat` /
`os.stat`, one bounded head read per listed report (`_read_head`) and
`scout_report.read_text` for a body — all **blocking**, so the daemon runs
`build` and `read` inside one `run_in_executor` hop each
(`BoardVerbsMixin.scout_reports_index` / `scout_report_body`).

**Where a report comes from — two facts and nothing else.** The dated
folders `<root>/scout/<YYYY-MM-DD>-<slug>/report.md` under every enrolled
root, and the cards' `report_path` column (`attach_report`'s ring), which
may name an older prose report outside `scout/`. A card whose report the
scan already reached *annotates* that row; it never duplicates it.

**The closed set is the body read's security boundary** (`locate`): a
client may send any string as `path`; only a card's stored `report_path`
(contained in that card's enrolled root) or `<enrolled root>/scout/<dated
folder>/report.md` (contained in that root) is ever read, both through the
attach's own `_plan_path_refusal` realpath rule — never "any `.md` under
an enrolled root".

**A text search reads the bodies, bounded** (`search`): the same
candidate set `build` lists (`_candidates`, one function both read), newest
first, at most `SEARCH_MAX_BODIES` bodies through `scout_report.read_text`
(64 KiB or nothing — an oversized or unreadable report is counted in
``unsearched`` and skipped, never clipped), stopping at `SEARCH_MAX_HITS`.
Only `_split`'s body is searched: the H1 and the answer block are the
index's and the client's instant filter's. Case and combining marks are
folded (`fold`). A hit is a `row` plus a one-line ``snippet`` and its
``match_line`` — never the text. Blocking, like `build`: one executor hop.

`scout_report` (byte-pinned three ways, run under Python 3.9 in the pack)
is **not** edited for this: the header split lives here and a test pins it
equal to `scout_report.parse_header`. Nothing here knows what a scout is
beyond the header keys, so the manual-check section can reuse the shapes.
"""

import os
import re
import stat
import unicodedata

from . import scout_report

#: A dated report folder: `YYYY-MM-DD-<slug>`.
FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[^/\n]+\Z")
#: At most this many enrolled roots are scanned.
MAX_ROOTS = 64
#: At most this many dated folders per root, the newest by folder name.
MAX_FOLDERS_PER_ROOT = 200
#: The head read for a row: enough for the H1 and a 40-line answer block.
INDEX_HEAD_BYTES = 16_384
#: The sealed page bound (`_knowledge_page_bytes`' budget); `build` stops
#: reading heads once the rows plainly exceed it, the API trims exactly.
PAGE_BYTES = 300_000

#: A text search needs at least this many characters (after `strip`);
#: `ScoutReportSearch.minBodyChars` on both apps is pinned equal to it.
MIN_QUERY_CHARS = 3
#: ... and at most this many.
MAX_QUERY_CHARS = 200
#: At most this many bodies are read per search, newest first.
SEARCH_MAX_BODIES = 400
#: The search stops at this many hits.
SEARCH_MAX_HITS = 50
#: A hit's snippet is at most this many characters.
SNIPPET_CHARS = 160

#: A search term under `MIN_QUERY_CHARS`.
QUERY_TOO_SHORT = "a text search needs at least 3 characters"
#: A search term over `MAX_QUERY_CHARS`.
QUERY_TOO_LONG = "a search must be at most 200 characters"

#: The body read's refusal for anything outside the closed set.
NOT_LISTED = "that report is not one Dark Army lists"
#: The body read's words when a listed file could not be read.
UNREADABLE = "that report could not be read"

_BLOCK_LINE = re.compile(
    r"^- \*\*(" + "|".join(re.escape(k) for k in scout_report.HEADER_KEYS)
    + r"):\*\*")
_H1 = re.compile(r"^# \S")
#: A rough per-row JSON size for `build`'s "plainly exceeded" stop.
_ROW_OVERHEAD = 400


def _refusal_default():
    # Imported late: `daemon_board` imports this module's siblings, and the
    # one containment rule is its `_plan_path_refusal` — never a second rule.
    from .daemon_board import BoardVerbsMixin
    return BoardVerbsMixin._plan_path_refusal


def _normalise(root: str) -> str:
    from . import dispatch
    return dispatch.normalise_root(root)


def _title(text: str) -> str:
    """The first H1's text, or ``""``."""
    for line in (text or "").splitlines():
        if _H1.match(line):
            return line[2:].strip()
    return ""


def _split(text: str):
    """``(title, header, body, found)`` — `split_header` plus whether an
    answer block was there at all (a block whose every value says "none"
    is still a block)."""
    text = text or ""
    lines = text.splitlines(keepends=True)
    h1 = None
    for index, line in enumerate(lines):
        if _H1.match(line):
            h1 = index
            break
    if h1 is None:
        return "", {}, text, False
    cursor = h1 + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    start = cursor
    while cursor < len(lines) and _BLOCK_LINE.match(lines[cursor].rstrip()):
        cursor += 1
    if cursor == start:
        return "", {}, text, False
    title = lines[h1][2:].strip()
    body = "".join(lines[:h1] + lines[cursor:]).lstrip("\r\n")
    return title, scout_report.parse_header(text), body, True


def split_header(text: str) -> tuple:
    """``(title, header, body)``: the H1's text, `scout_report.parse_header`
    of the text, and the text with the H1 and the answer block's lines
    removed. The block is found the way `scout_report` finds it — past the
    H1 and any blank lines, every consecutive `- **<Key>:**` line for a key
    in `scout_report.HEADER_KEYS`. No H1 or no block is ``("", {}, text)``."""
    title, header, body, _found = _split(text)
    return title, header, body


def _read_head(path: str, size: int) -> tuple:
    """``(text, whole)``: at most `INDEX_HEAD_BYTES` of the file, opened
    non-blocking, never through a final symlink, only a regular file —
    `scout_report.read_text`'s rules, so a FIFO swapped in cannot hang the
    executor. ``whole`` is True when the entire file fitted the head."""
    if size > scout_report.MAX_REPORT_BYTES:
        return "", False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except (OSError, TypeError, ValueError):
        return "", False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "", False
        payload = os.read(fd, INDEX_HEAD_BYTES + 1)
    except OSError:
        return "", False
    finally:
        os.close(fd)
    whole = len(payload) <= INDEX_HEAD_BYTES
    payload = payload[:INDEX_HEAD_BYTES]
    return payload.decode("utf-8", "ignore" if not whole else "replace"), whole


def _inside(base: str, resolved: str) -> bool:
    return bool(base) and resolved.startswith(base + os.sep)


def scan_root(root: str) -> list:
    """The dated report folders under `<root>/scout/`, newest folder name
    first, at most `MAX_FOLDERS_PER_ROOT`. Each entry is ``{"path",
    "folder", "stat"}`` — ``path`` the realpath of a `report.md` that is a
    regular file (no final symlink) inside the root. An undated folder, a
    symlinked folder, a missing `report.md`, a FIFO or a file resolving
    out of the root is skipped."""
    base = _normalise(root)
    if not base:
        return []
    folder_dir = os.path.join(base, scout_report.FOLDER)
    try:
        with os.scandir(folder_dir) as listing:
            names = [e.name for e in listing
                     if FOLDER_RE.match(e.name)
                     and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    names.sort(reverse=True)
    found = []
    for name in names[:MAX_FOLDERS_PER_ROOT]:
        candidate = os.path.join(folder_dir, name, scout_report.REPORT_NAME)
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        resolved = os.path.realpath(candidate)
        if not _inside(base, resolved):
            continue
        found.append({"path": resolved, "folder": name, "stat": st})
    return found


def row(path: str, root: str, card, st, head_text: str, *,
        whole: bool = False, project: str = "") -> dict:
    """One list row. **No `text` and no `body`**: the body is the second,
    separate read. ``checked`` is `scout_report.check`'s verdict only where
    the whole file fitted the head read, else ``None`` (unknown — never
    False)."""
    card = card or {}
    header = scout_report.parse_header(head_text)
    folder = os.path.basename(os.path.dirname(path))
    dated = FOLDER_RE.match(folder) and os.path.basename(
        os.path.dirname(os.path.dirname(path))).casefold() \
        == scout_report.FOLDER.casefold()
    return {
        "path": path,
        "root": root,
        "project": project or os.path.basename(root),
        "folder": folder if dated else "",
        "day": folder[:10] if dated else "",
        "written_at": float(getattr(st, "st_mtime", 0.0) or 0.0),
        "title": _title(head_text),
        "verdict": str(header.get("verdict") or ""),
        "confidence": str(header.get("confidence") or ""),
        "recommendation": str(header.get("recommendation") or ""),
        "question": str(header.get("question") or ""),
        "card_id": str(card.get("id") or ""),
        "card_title": str(card.get("title") or ""),
        "card_column": str(card.get("column_name") or ""),
        "checked": (not scout_report.check(head_text)) if whole else None,
        "bytes": int(getattr(st, "st_size", 0) or 0),
    }


def _card_root(card: dict) -> str:
    return _normalise(str(card.get("root") or ""))


def fold(text: str) -> str:
    """``text`` casefolded, with combining marks dropped (NFKD) where it is
    not plain ASCII — `ai_title.py`'s recipe without its replacements, so
    ``ł`` is left alone. ASCII takes the fast path: the per-character mark
    filter is the slow part and most reports are ASCII."""
    text = text or ""
    if text.isascii():
        return text.casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed
                   if not unicodedata.combining(c)).casefold()


def _candidates(roots, cards, refusal) -> tuple:
    """``(ordered, wanted_count)``: every report `build` lists, as
    ``{"path", "root", "stat", "card"}`` newest first (`st_mtime`
    descending, then `path`), and how many roots were scanned. The one
    candidate set `build` and `search` both read."""
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
                "card": None})
    for card in cards or ():
        stored = str(card.get("report_path") or "").strip()
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
        candidates[resolved] = {"path": resolved, "root": base,
                                "stat": st, "card": card}
    ordered = sorted(candidates.values(),
                     key=lambda c: (-float(c["stat"].st_mtime), c["path"]))
    return ordered, len(wanted)


def build(roots, cards, label_for=None, *, refusal=None) -> dict:
    """The whole list, newest first (`written_at` descending, then `path`).

    ``roots`` are enrolled roots; ``cards`` any iterable of card dicts
    (``id``, ``title``, ``column_name``, ``root``, ``report_path``);
    ``label_for(root)`` names a project. A card's `report_path` the scan
    did not reach — outside `scout/`, an older prose report — is its own
    row after `refusal(card root, path)` admits it, and only for a card
    whose root is one of ``roots``. Heads are read newest first and the
    reading stops once the rows plainly exceed `PAGE_BYTES`; ``omitted``
    counts the rest."""
    refusal = refusal or _refusal_default()
    label_for = label_for or (lambda r: os.path.basename(r))
    ordered, wanted_count = _candidates(roots, cards, refusal)
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
        head, whole = _read_head(candidate["path"],
                                 int(candidate["stat"].st_size))
        item = row(candidate["path"], base, candidate["card"],
                   candidate["stat"], head, whole=whole,
                   project=labels[base])
        spent += _ROW_OVERHEAD + sum(
            len(v) for v in item.values() if isinstance(v, str))
        rows.append(item)
    return {"supported": True, "available": True, "rows": rows,
            "truncated": omitted > 0, "omitted": omitted,
            "roots": wanted_count}


def snippet(body: str, needle_folded: str) -> tuple:
    """``(line_no, text)``: the first line of ``body`` (1-based) whose
    `fold` contains ``needle_folded``, whitespace collapsed and clipped to
    `SNIPPET_CHARS` around the match. The offset is found on the folded
    line and applied to the original: exact for an ASCII line, off by the
    marks dropped before the match on an accented one — **a snippet, not a
    citation**. ``(0, "")`` when no single line holds the needle."""
    if not needle_folded:
        return 0, ""
    for number, line in enumerate((body or "").splitlines(), start=1):
        collapsed = " ".join(line.split())
        offset = fold(collapsed).find(needle_folded)
        if offset < 0:
            continue
        if len(collapsed) <= SNIPPET_CHARS:
            return number, collapsed
        room = SNIPPET_CHARS - 2
        start = max(0, offset - max(0, (room - len(needle_folded)) // 2))
        start = min(start, max(0, len(collapsed) - room))
        end = start + room
        clipped = collapsed[start:end]
        if start > 0:
            clipped = "\u2026" + clipped
        if end < len(collapsed):
            clipped = clipped + "\u2026"
        return number, clipped
    return 0, ""


def search(roots, cards, query, label_for=None, *, refusal=None) -> dict:
    """The reports whose **body** holds ``query``, newest first, each a
    `row` plus ``snippet``, ``match_line`` and ``match: "body"``.

    ``query`` has its whitespace collapsed and is folded; under
    `MIN_QUERY_CHARS` (before or after folding) or over `MAX_QUERY_CHARS`
    is a `ValueError` in words. The candidates are
    `_candidates`' — the list `build` shows — read newest first, at most
    `SEARCH_MAX_BODIES` of them, each through `scout_report.read_text`:
    an empty answer (over 64 KiB, unreadable, not a regular file) counts in
    ``unsearched`` and is skipped, never clipped. The header block is not
    searched. The answer is `build`'s shape plus ``query``, ``searched``,
    ``unsearched``, ``search_truncated`` (the bodies cap bit before the
    candidates ran out) and ``hits_truncated`` (the hits cap did)."""
    refusal = refusal or _refusal_default()
    label_for = label_for or (lambda r: os.path.basename(r))
    # Whitespace collapsed the way `snippet` collapses a line, so a term
    # typed with a double space or a tab still finds its line.
    term = " ".join(str(query or "").split())
    if len(term) < MIN_QUERY_CHARS:
        raise ValueError(QUERY_TOO_SHORT)
    if len(term) > MAX_QUERY_CHARS:
        raise ValueError(QUERY_TOO_LONG)
    needle = fold(term)
    # A term of combining marks alone folds to (nearly) nothing and would
    # match every body: the bound is on what is searched for.
    if len(needle.strip()) < MIN_QUERY_CHARS:
        raise ValueError(QUERY_TOO_SHORT)
    ordered, wanted_count = _candidates(roots, cards, refusal)
    rows = []
    labels = {}
    searched = 0
    unsearched = 0
    search_truncated = False
    hits_truncated = False
    for index, candidate in enumerate(ordered):
        if index >= SEARCH_MAX_BODIES:
            search_truncated = True
            break
        text = scout_report.read_text(candidate["path"])
        if not text:
            unsearched += 1
            continue
        searched += 1
        _title_text, _header, body, _found = _split(text)
        if needle not in fold(body):
            continue
        # Only a hit past the cap says the cap cut something: stopping at
        # the cap with candidates left would claim a cut that may not be.
        if len(rows) >= SEARCH_MAX_HITS:
            hits_truncated = True
            break
        base = candidate["root"]
        if base not in labels:
            labels[base] = str(label_for(base) or os.path.basename(base))
        item = row(candidate["path"], base, candidate["card"],
                   candidate["stat"], text, whole=True, project=labels[base])
        line_no, words = snippet(body, needle)
        item.update({"snippet": words, "match_line": line_no,
                     "match": "body"})
        rows.append(item)
    return {"supported": True, "available": True, "rows": rows,
            "truncated": False, "omitted": 0, "roots": wanted_count,
            "query": term, "searched": searched, "unsearched": unsearched,
            "search_truncated": search_truncated,
            "hits_truncated": hits_truncated}


def locate(path: str, roots, cards, *, refusal=None) -> tuple:
    """``(resolved, root, refusal_words)`` for a body read — the closed set
    re-checked **at the moment of the read**, never trusting the row the
    client held. Admitted: a card's stored `report_path` (its root one of
    ``roots``, contained by that root), or `<root>/scout/<dated
    folder>/report.md` under one of ``roots`` (contained by that root).
    Anything else is `NOT_LISTED`."""
    refusal = refusal or _refusal_default()
    text = str(path or "").strip()
    if not text or len(text) > 4096 or not os.path.isabs(text):
        return "", "", NOT_LISTED
    members = {b for b in (_normalise(str(r or "")) for r in roots or ()) if b}
    target = os.path.realpath(text)
    for card in cards or ():
        stored = str(card.get("report_path") or "").strip()
        if not stored:
            continue
        base = _card_root(card)
        if base not in members:
            continue
        if stored != text and os.path.realpath(stored) != target:
            continue
        resolved, why = refusal(base, stored)
        if resolved and not why:
            return resolved, base, ""
    for base in sorted(members):
        if not _inside(base, target):
            continue
        parts = target[len(base) + len(os.sep):].split(os.sep)
        if (len(parts) == 3
                and parts[0].casefold() == scout_report.FOLDER.casefold()
                and FOLDER_RE.match(parts[1])
                and parts[2] == scout_report.REPORT_NAME):
            resolved, why = refusal(base, target)
            if resolved and not why:
                return resolved, base, ""
    return "", "", NOT_LISTED


def read(path: str, roots, cards, label_for=None, *, refusal=None) -> dict:
    """One report, split: ``{"available", "path", "root", "project",
    "title", "header", "body", "has_header", "checked", "written_at",
    "reason"}``. `locate` first; `scout_report.read_text` on the resolved
    path (whole file, bounded, no final symlink, regular file only)."""
    label_for = label_for or (lambda r: os.path.basename(r))
    resolved, root, why = locate(path, roots, cards, refusal=refusal)
    answer = {"available": False, "path": str(path or ""), "root": root,
              "project": "", "title": "", "header": {}, "body": "",
              "has_header": False, "checked": None, "written_at": 0.0,
              "reason": why}
    if why:
        return answer
    answer["path"] = resolved
    answer["project"] = str(label_for(root) or os.path.basename(root))
    text = scout_report.read_text(resolved)
    if not text:
        answer["reason"] = UNREADABLE
        return answer
    title, header, body, found = _split(text)
    try:
        written_at = float(os.stat(resolved).st_mtime)
    except OSError:
        written_at = 0.0
    answer.update({
        "available": True,
        "title": title or _title(text),
        "header": header,
        "body": body,
        "has_header": found,
        "checked": not scout_report.check(text),
        "written_at": written_at,
        "reason": "",
    })
    return answer
