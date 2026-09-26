"""The manual check's shape: one parser, checker, writer and scanner.

An implement run that leaves a check only a person can do writes it as
`manual-check/<YYYY-MM-DD>-<slug>/check.md` at the project root, captures
beside it. The file opens with an answer block under its H1 -- a Markdown
list of `- **Key:** value` lines, the plan template's own header shape --
then the two H2 headings in `HEADINGS`, in order:

    # <title>

    - **Card:** <the card's title>
    - **Project:** <project>
    - **Check:** <what is being checked, one line>
    - **Created:** 2026-09-25T14:32:00+02:00
    - **Status:** open
    - **Outcome:** none
    - **Checked at:** none

    ## Steps

    1. <one action per line>

    ## Why not automated

    <one line at least>

`Status`, `Outcome` and `Checked at` are the three lines Dark Army rewrites
when a person records Passed or Failed (`write_outcome`); every other byte
is the agent's and is never touched.

This one file serves three callers, so it imports nothing but the standard
library and parses under Python 3.9:

* Dark Army's daemon imports it (`dark_army_daemon.manual_check`): the flag
  refuses a file under `manual-check/` that fails `check`, the Checks
  section is `scan` over every enrolled root, and the outcome press is
  `write_outcome`.
* Dark Army's own checkout runs a byte copy as
  `python3 .claude/skills/ship/manual_check.py <check.md>`.
* The agent pack ships the same byte copy to every project it keeps in
  step (`template/.claude/skills/ship/manual_check.py`), where it runs under
  whatever `python3` the project has, the 3.9 system one included.

Edit this file and copy it over the other two; never edit a copy.
`host/tests/test_manual_check_file.py` pins the three byte-identical. The
pack renderer substitutes double-brace placeholders in every template file,
so this source must never contain two opening braces in a row.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: The folder at the project root that holds every manual check.
FOLDER = "manual-check"
#: The check's own file name inside its dated folder.
CHECK_NAME = "check.md"

#: The answer block's keys, in the order a check writes them. All required.
HEADER_KEYS = ("Card", "Project", "Check", "Created", "Status", "Outcome",
               "Checked at")
STATUSES = ("open", "passed", "failed")
#: The statuses a person may record; `open` is the only one they replace.
OUTCOMES = ("passed", "failed")

#: The two H2 headings after the answer block, in order.
HEADINGS = ("Steps", "Why not automated")

MAX_HEADER_LINES = 20
#: The read bound for a whole check.
MAX_CHECK_BYTES = 64 * 1024
#: The scan's bound per file: the head is enough to list a check.
MAX_HEAD_BYTES = 8 * 1024
#: At most this many checks listed per project.
MAX_CHECKS_PER_ROOT = 200
#: How much of `## Steps` a listed row carries (search and preview).
STEPS_PREVIEW_CHARS = 400
#: The person's note, clamped.
MAX_NOTE_CHARS = 400

#: Words that say "nothing here" (the plan header's rule).
NONE_WORDS = ("", "none", "n/a", "-", "—")

# The checker's sentences, fixed so the tests and the implement reference
# can quote them.
MISSING = "missing %s"
BAD_STATUS = "status must be one of " + ", ".join(STATUSES)
BAD_CREATED = ("created must be an ISO 8601 time, e.g. "
               "2026-09-25T14:32:00+02:00")
NO_STEPS = 'no numbered step ("1. ...") under "## Steps"'
NO_REASON = 'nothing under "## Why not automated"'
HEADING_MISSING = 'heading "## %s" missing'
HEADING_OUT_OF_ORDER = 'heading "## %s" out of order'
HEADER_TOO_LONG = "header longer than %d lines" % MAX_HEADER_LINES
OK = "ok"

# `write_outcome`'s refusals.
RECORDED = "that check already has an outcome"
BAD_OUTCOME = "an outcome is passed or failed"
UNREADABLE = "that check cannot be read"
MALFORMED = "that check is not in the manual-check shape"

_HEADER_LINE = re.compile(
    r"^- \*\*(" + "|".join(re.escape(k) for k in HEADER_KEYS)
    + r"):\*\*\s*(.*)$")
_H1 = re.compile(r"^# (\S.*)$")
_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_STEP = re.compile(r"^\s*\d+\.\s+\S")
#: `Created` as the Swift side's `ISO8601DateFormatter` reads it: a date, a
#: `T`, a time with seconds, an optional fraction and an offset or `Z`.
_CREATED = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|z|[+-]\d{2}:\d{2})$")


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _blank(value: str) -> bool:
    """Empty after trimming, or a `<placeholder>` left from the example."""
    text = value.strip()
    return not text or _is_placeholder(text)


def _nothing(value: str) -> bool:
    """`_blank`, or one of the words that say "none"."""
    return _blank(value) or value.strip().lower() in NONE_WORDS


def _header_span(lines: list) -> tuple:
    """`(start, end)` indices of the answer block in `lines`, or `(-1, -1)`.

    After the first `# ` line, blank lines are skipped, then every
    consecutive line matching a known key is the block, up to the first line
    that does not. A check with no H1 has no block."""
    start = None
    for index, line in enumerate(lines):
        if _H1.match(line.rstrip("\r\n")):
            start = index + 1
            break
    if start is None:
        return -1, -1
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = start
    while end < len(lines) and _HEADER_LINE.match(lines[end].rstrip("\r\n")):
        end += 1
    return start, end


def _header_entries(text: str) -> list:
    """The answer block's `(key, value)` pairs in order."""
    lines = (text or "").splitlines()
    start, end = _header_span(lines)
    entries = []
    for line in lines[start:end] if start >= 0 else []:
        match = _HEADER_LINE.match(line.rstrip())
        if match:
            entries.append((match.group(1), match.group(2).strip()))
    return entries


def _name(key: str) -> str:
    return key.lower().replace(" ", "_").replace("-", "_")


def parse_header(text: str) -> dict:
    """The answer block as a dict, with only what the check states.

    Keys are lowercased with `_` (`card`, `project`, `check`, `created`,
    `status`, `outcome`, `checked_at`). A blank, `<placeholder>` or "none"
    value is absent. A key written twice keeps its first value. `status` is
    lowercased."""
    found: dict = {}
    for key, value in _header_entries(text):
        if _nothing(value):
            continue
        name = _name(key)
        if name in found:
            continue
        if key == "Status":
            value = value.lower()
        found[name] = value
    return found


def title(text: str) -> str:
    """The H1's words, or `""`."""
    for line in (text or "").splitlines():
        match = _H1.match(line.rstrip())
        if match:
            return match.group(1).strip()
    return ""


def _sections(text: str) -> list:
    """Every H2 outside a fenced block as `(title, body_lines)`, in order."""
    out: list = []
    fenced = False
    for line in (text or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            if out:
                out[-1][1].append(line)
            continue
        match = None if fenced else _H2.match(line.rstrip())
        if match:
            out.append((match.group(1).strip(), []))
        elif out:
            out[-1][1].append(line)
    return out


def _section_body(text: str, heading: str) -> list:
    for name, body in _sections(text):
        if name == heading:
            return body
    return []


def steps_text(text: str) -> str:
    """The `## Steps` body, trimmed; `""` where there is none."""
    return "\n".join(_section_body(text, "Steps")).strip()


def _parse_time(value: str):
    """`datetime.fromisoformat`, with a trailing `Z` read as UTC first
    (Python before 3.11 refuses the `Z`); `None` when it will not parse."""
    text = str(value or "").strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _valid_created(value: str) -> bool:
    """A full date-time with an offset: what both sorters read the same way.
    A bare date, a time without seconds or an offset-less time is refused,
    because the phone and the panel would sort it last."""
    text = str(value or "").strip()
    return bool(_CREATED.match(text)) and _parse_time(text) is not None


def check(text: str) -> list:
    """Every way `text` falls short of the check shape, one sentence each,
    in a fixed order; `[]` when it is a well-formed check."""
    entries = _header_entries(text)
    first: dict = {}
    for key, value in entries:
        first.setdefault(key, value)
    problems = []
    for key in HEADER_KEYS:
        if key not in first or _blank(first[key]):
            problems.append(MISSING % key)
    status = first.get("Status", "")
    if not _blank(status) and status.strip().lower() not in STATUSES:
        problems.append(BAD_STATUS)
    created = first.get("Created", "")
    if not _blank(created) and not _valid_created(created):
        problems.append(BAD_CREATED)
    found = [name for name, _body in _sections(text)]
    last = -1
    present = []
    for heading in HEADINGS:
        if heading not in found:
            problems.append(HEADING_MISSING % heading)
            continue
        present.append(heading)
        position = found.index(heading)
        if position < last:
            problems.append(HEADING_OUT_OF_ORDER % heading)
        else:
            last = position
    if "Steps" in present:
        body = _section_body(text, "Steps")
        if not any(_STEP.match(line) for line in body):
            problems.append(NO_STEPS)
    if "Why not automated" in present:
        body = _section_body(text, "Why not automated")
        if not any(line.strip() for line in body):
            problems.append(NO_REASON)
    if len(entries) > MAX_HEADER_LINES:
        problems.append(HEADER_TOO_LONG)
    return problems


def brief(problems: list, limit: int = 3) -> str:
    """The problems as one short line for a refusal: every `missing` key
    folded into one phrase, then at most `limit` items, the rest counted."""
    missing = []
    rest = []
    prefix = MISSING % ""
    for problem in problems:
        if problem.startswith(prefix):
            missing.append(problem[len(prefix):])
        else:
            rest.append(problem)
    items = ([prefix + ", ".join(missing)] if missing else []) + rest
    shown = items[:limit]
    more = len(items) - len(shown)
    text = "; ".join(shown)
    if more > 0:
        text += "; and %d more" % more
    return text


def _read_bytes(path, bound: int, truncate: bool):
    """The file's bytes up to `bound`; `None` on any failure.

    Opened non-blocking, never through a final symlink, and read only when
    the opened descriptor is a regular file: a FIFO put where the check was
    would otherwise hang the open (or the read) for ever. Over the bound is
    `None` unless `truncate`, when the first `bound` bytes come back."""
    try:
        fd = os.open(os.fspath(path),
                     os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except (OSError, TypeError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        remaining = bound + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > bound:
        if not truncate:
            return None
        payload = payload[:bound]
    return payload


def read_text(path, bound: int = MAX_CHECK_BYTES) -> str:
    """The file's text, bounded; `""` on any failure or over the bound
    (`scout_report.read_text`'s rule)."""
    payload = _read_bytes(path, bound, False)
    if payload is None:
        return ""
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return ""


def read_header(path) -> dict:
    """`parse_header` of the file at `path`, read bounded; `{}` on any
    failure -- failure is no evidence, never an error."""
    return parse_header(read_text(path))


def _one_line(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def outcome_text(note: str) -> str:
    """The `Outcome` value a note is written as: one line, clamped; `none`
    for an empty note; and a note the parser would read as "nothing" -- a
    none-word (`none`, `n/a`, `-`) or a `<placeholder>` -- is quoted, so the
    written file still passes `check` and the person's words survive."""
    text = _one_line(note, MAX_NOTE_CHARS)
    if not text:
        return "none"
    if _nothing(text):
        return '"%s"' % text
    return text


def _stamp(when) -> str:
    """ISO 8601 with seconds and a colon in the offset."""
    if not isinstance(when, datetime):
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.astimezone()
    return when.replace(microsecond=0).isoformat()


def write_outcome(path, status: str, note: str = "", when=None) -> str:
    """Record a person's Passed or Failed in the file itself. `""` when
    written, else the refusal in words; a refusal writes nothing.

    Re-reads the file (the list the person pressed on may be stale),
    refuses anything not in the check shape and any `Status` but `open`
    (`RECORDED`), then rewrites exactly the `Status`, `Outcome` and
    `Checked at` lines of the answer block -- `Outcome` is `none` for an
    empty note -- through a sibling temporary file and `os.replace`, the
    file's mode kept and every other byte untouched."""
    wanted = str(status or "").strip().lower()
    if wanted not in OUTCOMES:
        return BAD_OUTCOME
    payload = _read_bytes(path, MAX_CHECK_BYTES, False)
    if payload is None:
        return UNREADABLE
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        return UNREADABLE
    if check(text):
        return MALFORMED
    if parse_header(text).get("status", "") != "open":
        return RECORDED
    written = outcome_text(note)
    values = {"Status": wanted, "Outcome": written,
              "Checked at": _stamp(when)}
    lines = text.splitlines(keepends=True)
    start, end = _header_span(lines)
    done: set = set()
    for index in range(start, end):
        line = lines[index]
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = _HEADER_LINE.match(body)
        if not match or match.group(1) not in values:
            continue
        key = match.group(1)
        if key in done:
            continue
        done.add(key)
        lines[index] = "- **%s:** %s%s" % (key, values[key], ending)
    if len(done) != len(values):
        return MALFORMED
    target = os.fspath(path)
    try:
        mode = stat.S_IMODE(os.lstat(target).st_mode)
    except OSError:
        return UNREADABLE
    folder = os.path.dirname(target) or "."
    fd, temp = tempfile.mkstemp(prefix=".check-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write("".join(lines).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        return UNREADABLE
    return ""


def _entry(path: str, folder: str, text: str, problems: list) -> dict:
    header = parse_header(text)
    entry = {"path": path, "folder": folder, "title": title(text)}
    for key in HEADER_KEYS:
        entry[_name(key)] = str(header.get(_name(key), ""))
    entry["steps_preview"] = steps_text(text)[:STEPS_PREVIEW_CHARS]
    entry["malformed"] = bool(problems)
    entry["problem"] = problems[0] if problems else ""
    return entry


def scan(root, limit: int = MAX_CHECKS_PER_ROOT) -> list:
    """Every check under `<root>/manual-check/`, at most `limit`, in
    `sort_key` order (newest first).

    One dict per `check.md`: `path`, `folder`, `title` (the H1), the seven
    header keys lowercased (`""` where none), `steps_preview`, `malformed`
    and `problem`. A `manual-check` folder that is a symlink lists nothing.
    A dated folder that is a symlink, and a `check.md` that
    is not a regular file, are skipped; a file failing `check` is listed
    `malformed` with its first problem, never dropped. Each file is read
    through `_read_bytes`' non-blocking, no-symlink open: its head alone,
    and the whole file only when the head was not all of it."""
    base = os.path.join(os.fspath(root), FOLDER)
    # The folder itself must be a real directory: a `manual-check` link
    # would list (and let a press reach) checks from anywhere it points.
    try:
        if not stat.S_ISDIR(os.lstat(base).st_mode):
            return []
    except OSError:
        return []
    names = []
    try:
        with os.scandir(base) as found:
            for item in found:
                try:
                    if item.is_dir(follow_symlinks=False):
                        names.append(item.name)
                except OSError:
                    continue
    except OSError:
        return []
    entries = []
    for name in names:
        path = os.path.join(base, name, CHECK_NAME)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
        except OSError:
            continue
        head = _read_bytes(path, MAX_HEAD_BYTES, True)
        if head is None:
            continue
        payload = head
        if len(head) >= MAX_HEAD_BYTES:
            payload = _read_bytes(path, MAX_CHECK_BYTES, False) or b""
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            text = ""
        problems = check(text) if text else [UNREADABLE]
        entries.append(_entry(path, name, text, problems))
    entries.sort(key=sort_key, reverse=True)
    return entries[:max(0, int(limit))]


def sort_key(entry: dict) -> tuple:
    """`(created as a timestamp, folder)`: sort with `reverse=True` for
    newest first. An unreadable `Created` sorts oldest."""
    moment = _parse_time(entry.get("created", ""))
    stamp = 0.0
    if moment is not None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        stamp = moment.timestamp()
    return (stamp, str(entry.get("folder", "")))


def matches(entry: dict, query: str) -> bool:
    """Casefolded substring over title, check, steps, outcome and card; an
    empty query matches every entry."""
    needle = str(query or "").strip().casefold()
    if not needle:
        return True
    for key in ("title", "check", "steps_preview", "outcome", "card"):
        if needle in str(entry.get(key, "")).casefold():
            return True
    return False


def main(argv: list) -> int:
    """`manual_check.py <check.md>`: print `ok` and exit 0, or one problem
    per line and exit 1."""
    if len(argv) != 1:
        print("usage: python3 .claude/skills/ship/manual_check.py "
              "manual-check/<YYYY-MM-DD>-<slug>/" + CHECK_NAME,
              file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        size = path.stat().st_size
    except OSError:
        print("cannot read %s" % argv[0])
        return 1
    if size > MAX_CHECK_BYTES:
        print("check larger than %d KiB" % (MAX_CHECK_BYTES // 1024))
        return 1
    text = read_text(path)
    if not text and size:
        print("cannot read %s (not a regular file, or not UTF-8 text)"
              % argv[0])
        return 1
    problems = check(text)
    if not problems:
        print(OK)
        return 0
    for problem in problems:
        print(problem)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
