"""The scout report's shape: one parser and one checker.

A scout run (`/scout`, alias `/ship scout`) writes `scout/<YYYY-MM-DD>-<slug>/report.md`
at the project root. The report opens with an answer block under its H1 --
a Markdown list of `- **Key:** value` lines, the plan template's own header
shape -- and then the five H2 headings in `HEADINGS`, in order:

    # <title>

    - **Card:** <the card's title>
    - **Project:** <project>
    - **Question:** <the question, one line>
    - **Verdict:** <the answer, one line>
    - **Confidence:** high | medium | low
    - **Recommendation:** build | do-not-build | needs-decision | more-scouting
    - **Follow-up:** <title> — <one-line summary>   (zero or more lines)
    - **Sources:** <comma-separated, or none>

This one file serves three callers, so it imports nothing but the standard
library and parses under Python 3.9:

* Dark Army's daemon imports it (`dark_army_daemon.scout_report`): the
  attach refuses a report under `scout/` that fails `check`, and Promote
  reads `read_header` to compose the build card.
* Dark Army's own checkout runs a byte copy as
  `python3 .claude/skills/scout/scout_check.py <report>`.
* The agent pack ships the same byte copy to every project it keeps in
  step (`template/.claude/skills/scout/scout_check.py`), where it runs under
  whatever `python3` the project has, the 3.9 system one included.

Edit this file and copy it over the other two; never edit a copy.
`host/tests/test_scout_report.py` pins the three byte-identical. The pack
renderer substitutes double-brace placeholders in every template file, so
this source must never contain two opening braces in a row.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

#: The folder at the project root that holds every scout report.
FOLDER = "scout"
#: The report's own file name inside its dated folder.
REPORT_NAME = "report.md"

#: The answer block's keys, in the order a report writes them.
HEADER_KEYS = ("Card", "Project", "Question", "Verdict", "Confidence",
               "Recommendation", "Follow-up", "Sources")
#: `Follow-up` is optional and may repeat; every other key is required once.
OPTIONAL_KEYS = ("Follow-up",)
REQUIRED_KEYS = tuple(k for k in HEADER_KEYS if k not in OPTIONAL_KEYS)

CONFIDENCES = ("high", "medium", "low")
RECOMMENDATIONS = ("build", "do-not-build", "needs-decision", "more-scouting")

#: The five H2 headings after the answer block, in order.
HEADINGS = ("Question", "What was found", "Evidence", "Recommendation",
            "Open questions")

MAX_HEADER_LINES = 40
#: The read bound, the daemon's plan bound restated here so this file
#: imports nothing of the daemon's.
MAX_REPORT_BYTES = 64 * 1024

#: The separator between a follow-up's title and its summary.
FOLLOW_UP_SEPARATOR = " — "

#: Words that say "nothing here" (the plan header's rule).
NONE_WORDS = ("", "none", "n/a", "-", "—")

# The checker's sentences, fixed so the tests and the scout reference can
# quote them.
MISSING = "missing %s"
BAD_CONFIDENCE = "confidence must be one of " + ", ".join(CONFIDENCES)
BAD_RECOMMENDATION = ("recommendation must be one of "
                      + ", ".join(RECOMMENDATIONS))
EMPTY_VERDICT = "verdict is empty"
BAD_FOLLOW_UP = 'follow-up line needs "<title>' + FOLLOW_UP_SEPARATOR + '<summary>"'
HEADING_MISSING = 'heading "## %s" missing'
HEADING_OUT_OF_ORDER = 'heading "## %s" out of order'
HEADER_TOO_LONG = "header longer than %d lines" % MAX_HEADER_LINES
OK = "ok"

_HEADER_LINE = re.compile(
    r"^- \*\*(" + "|".join(re.escape(k) for k in HEADER_KEYS)
    + r"):\*\*\s*(.*)$")
_H1 = re.compile(r"^# \S")
_H2 = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _is_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _blank(value: str) -> bool:
    """Empty after trimming, or a `<placeholder>` left from the example."""
    text = value.strip()
    return not text or _is_placeholder(text)


def _nothing(value: str) -> bool:
    """`_blank`, or one of the words that say "none"."""
    return _blank(value) or value.strip().lower() in NONE_WORDS


def _header_entries(text: str) -> list:
    """The answer block's `(key, value)` pairs in order.

    After the first `# ` line, blank lines are skipped, then every
    consecutive line matching a known key is the block, up to the first
    line that does not. A report with no H1 has no block."""
    lines = (text or "").splitlines()
    start = None
    for index, line in enumerate(lines):
        if _H1.match(line):
            start = index + 1
            break
    if start is None:
        return []
    while start < len(lines) and not lines[start].strip():
        start += 1
    entries = []
    for line in lines[start:]:
        match = _HEADER_LINE.match(line.rstrip())
        if not match:
            break
        entries.append((match.group(1), match.group(2).strip()))
    return entries


def _split_follow_up(value: str) -> dict:
    title, sep, summary = value.partition(FOLLOW_UP_SEPARATOR)
    if not sep:
        return {"title": value.strip(), "summary": ""}
    return {"title": title.strip(), "summary": summary.strip()}


def parse_header(text: str) -> dict:
    """The answer block as a dict, with only what the report states.

    Keys are lowercased with `_` (`card`, `project`, `question`,
    `verdict`, `confidence`, `recommendation`, `sources`,
    `follow_ups`). A blank, `<placeholder>` or "none" value is absent.
    `follow_ups` is a list of `{"title", "summary"}`; `sources` a list of
    strings. A key written twice keeps its first value. `confidence` and
    `recommendation` are lowercased."""
    found: dict = {}
    follow_ups = []
    for key, value in _header_entries(text):
        if _nothing(value):
            continue
        if key == "Follow-up":
            follow_ups.append(_split_follow_up(value))
            continue
        name = key.lower().replace("-", "_")
        if name in found:
            continue
        if key == "Sources":
            items = [part.strip() for part in value.split(",")]
            items = [part for part in items if not _nothing(part)]
            if items:
                found[name] = items
            continue
        if key in ("Confidence", "Recommendation"):
            value = value.lower()
        found[name] = value
    if follow_ups:
        found["follow_ups"] = follow_ups
    return found


def _headings(text: str) -> list:
    """Every H2 title outside a fenced block, in order."""
    out = []
    fenced = False
    for line in (text or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _H2.match(line.rstrip())
        if match:
            out.append(match.group(1).strip())
    return out


def check(text: str) -> list:
    """Every way `text` falls short of the report shape, one sentence each,
    in a fixed order; `[]` when it is a well-formed report."""
    entries = _header_entries(text)
    first: dict = {}
    for key, value in entries:
        first.setdefault(key, value)
    problems = []
    for key in REQUIRED_KEYS:
        if key not in first or _blank(first[key]):
            problems.append(MISSING % key)
    confidence = first.get("Confidence", "")
    if not _blank(confidence) and confidence.strip().lower() not in CONFIDENCES:
        problems.append(BAD_CONFIDENCE)
    recommendation = first.get("Recommendation", "")
    if (not _blank(recommendation)
            and recommendation.strip().lower() not in RECOMMENDATIONS):
        problems.append(BAD_RECOMMENDATION)
    verdict = first.get("Verdict", "")
    if not _blank(verdict) and _nothing(verdict):
        problems.append(EMPTY_VERDICT)
    for key, value in entries:
        # Only an empty or "none" line says "no follow-up"; the example line
        # left unfilled, or half-filled (`Fix X — <one-line summary>`), is as
        # wrong as a line with no separator at all.
        if key != "Follow-up" or value.strip().lower() in NONE_WORDS:
            continue
        parts = _split_follow_up(value)
        if _blank(parts["title"]) or _blank(parts["summary"]):
            problems.append(BAD_FOLLOW_UP)
            break
    found = _headings(text)
    last = -1
    for heading in HEADINGS:
        if heading not in found:
            problems.append(HEADING_MISSING % heading)
            continue
        position = found.index(heading)
        if position < last:
            problems.append(HEADING_OUT_OF_ORDER % heading)
        else:
            last = position
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


def read_text(path) -> str:
    """The file's text, bounded; `""` on any failure or over the bound.

    Opened non-blocking, never through a final symlink, and read only when
    the opened descriptor is a regular file: a FIFO put where the report was
    would otherwise hang the open (or the read) for ever, and the daemon
    reads this at the attach and at a press. The daemon passes a realpath,
    so refusing a link costs it nothing."""
    try:
        fd = os.open(os.fspath(path),
                     os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except (OSError, TypeError, ValueError):
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        chunks = []
        remaining = MAX_REPORT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return ""
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > MAX_REPORT_BYTES:
        return ""
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return ""


def read_header(path) -> dict:
    """`parse_header` of the file at `path`, read bounded; `{}` on any
    failure -- failure is no evidence, never an error."""
    return parse_header(read_text(path))


def main(argv: list) -> int:
    """`scout_check.py <report.md>`: print `ok` and exit 0, or one problem
    per line and exit 1."""
    if len(argv) != 1:
        print("usage: python3 .claude/skills/scout/scout_check.py "
              "scout/<YYYY-MM-DD>-<slug>/" + REPORT_NAME, file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        size = path.stat().st_size
    except OSError:
        print("cannot read %s" % argv[0])
        return 1
    if size > MAX_REPORT_BYTES:
        print("report larger than %d KiB" % (MAX_REPORT_BYTES // 1024))
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
