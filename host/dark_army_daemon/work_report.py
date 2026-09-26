"""A finished agent's `## Work done` report, split into the parts it names.

`hooks.WORK_REPORT_HINT` dictates the shape — `**Asked:**`, `**Changed:**`,
`**Verified:**`, `**Unchecked:**` (numbered steps and one line beginning
`Why not automated:`, or exactly `Nothing - every check above ran.`) and an
optional `**Card:**` — and `session_stats._work_report` slices it off the
closing message into `last_report`. This module reads that slice once, on
the snapshot executor, so the row, the banner, the inbox and both clients'
details draw **one** parsed shape and no client re-parses the text.

Two things it never does:

* **Infer state.** Whether a turn is finished, who it is waiting on, which
  bucket a row is in and what `finish_word` says are decided elsewhere
  (`session_stats.finished_quietly`, `categorize`, `finish_word`) and are
  untouched by anything here. This structures what is *drawn*.
* **Read a file.** It is handed a string of at most
  `session_stats.MAX_LAST_REPORT_CHARS` and returns a dict. Pure, stdlib
  only (`re`, `functools`, `typing`), imports nothing from the daemon.

A report that does not follow the labelled shape parses to
`labelled: False` with every part empty and an empty headline, and both
clients then draw the raw report exactly as they did before. A report the
daemon cut from the head (`cut: True`, it leads with `…`) may lack its
Asked part; nothing is invented to fill it.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

#: The labels the hint dictates, in the order it dictates them.
LABELS = ("Asked", "Changed", "Verified", "Unchecked", "Card")

#: The one sentence that means "every check ran". Tolerant of the three
#: dashes a model may type, a missing full stop and bold around it.
NOTHING_UNCHECKED_RE = re.compile(
    r"^\W*nothing\s*[-–—]\s*every check above ran\W*$",
    re.IGNORECASE)

#: The line that follows the numbered steps and says why they are manual.
WHY_NOT_RE = re.compile(r"^Why not automated:", re.IGNORECASE)

#: The one line drawn where space is small: a banner body, a row's lead.
HEADLINE_CHARS = 120

#: Items kept per list; a report is under 25 lines, so more is a runaway.
MAX_ITEMS = 12

#: Matched against a *stripped* line, and no two whitespace quantifiers are
#: ever adjacent: a report is agent-written text, parsed on every agents
#: push, and `\s+(?:\*\*)?\s*` over a long run of blanks backtracks
#: quadratically.
#: `**Label:**` and plain `Label:` open a section; a bulleted label only
#: when it is bold (`- **Card:**`), so a change written `- Card: …` under
#: Changed stays an item of Changed.
_LABEL_RE = re.compile(
    r"^(?:[-*+]\s+(?:\*\*|__)|(?:\*\*|__)?)(" + "|".join(LABELS) + r")"
    r"(?:\*\*|__)?\s*:(?:\*\*|__)?\s*(.*)$")
#: Matched against a line with its leading blanks already taken off.
_BULLET_RE = re.compile(r"^(?:[-*+•]|\d{1,3}[.)])\s+(.*)$")
#: A code-fence line: a model fencing its steps must not make the fence a step.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
#: Reports kept parsed, keyed on the text: a report parses once, not once
#: per agents push. Bounded; a later prompt clears the report anyway.
PARSE_CACHE = 64
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_WS_RE = re.compile(r"\s+")
_DECORATION_RE = re.compile(r"\*\*|__|`")


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _plain(text: str) -> str:
    """Bold, underline-bold and code ticks off, whitespace collapsed — the
    form a banner and a one-line row can draw."""
    return _collapse(_DECORATION_RE.sub("", text))


def _lead(text: str) -> str:
    """A part as the headline's lead: plain, and without the full stop a
    sentence ends on, since `·` follows it."""
    plain = _plain(text)
    return plain[:-1].rstrip() if plain.endswith(".") and not plain.endswith("..") \
        else plain


def _items(lines: list[str]) -> list[str]:
    """A section's lines as list items.

    A bullet (`-`, `*`, `+`, `•`, `1.`, `1)`) starts an item and an
    unbulleted line continues the current one; a blank line ends it. So a
    bare paragraph is one item and a list is one item per bullet. The text
    on the label's own line is the first item — unless it ends in `:` and a
    list follows it, when it is only the list's lead-in.
    """
    items: list[str] = []
    current: list[str] = []
    bulleted = False

    def close() -> None:
        if current:
            text = _collapse(" ".join(current))
            if text:
                items.append(text)
            current.clear()

    lines = [line for line in lines if not _FENCE_RE.match(line)]
    for line in lines:
        if not line.strip():
            close()
            continue
        bullet = _BULLET_RE.match(line.lstrip())
        if bullet:
            close()
            bulleted = True
            current.append(bullet.group(1))
        elif WHY_NOT_RE.match(line.strip().lstrip("*_ ")):
            # The reason line is never a numbered step's continuation.
            close()
            current.append(line.strip())
        else:
            current.append(line.strip())
    close()
    if bulleted and len(items) > 1 and lines and lines[0].strip() \
            and not _BULLET_RE.match(lines[0].lstrip()) and items[0].endswith(":"):
        items = items[1:]
    return items


def _empty(cut: bool = False) -> dict:
    return {"labelled": False, "cut": cut, "asked": "", "changed": [],
            "verified": [], "unchecked": [], "nothing_unchecked": False,
            "why_not_automated": "", "card": "", "headline": "",
            "changed_total": 0, "verified_total": 0, "unchecked_total": 0}


def parse(text: str) -> dict:
    """The report's parts, or an honest `labelled: False`.

    `{"labelled", "cut", "asked", "changed", "verified", "unchecked",
    "nothing_unchecked", "why_not_automated", "card", "headline",
    "changed_total", "verified_total", "unchecked_total"}` — lists for
    Changed, Verified and Unchecked (at most `MAX_ITEMS` each, with the
    true count beside each in `*_total`), strings for the rest. A section
    runs from its label's line to the next label; text before the first
    label (the heading, or the tail of a cut section) belongs to no section.
    Memoised on the text (`PARSE_CACHE`); every call gets its own copy.
    """
    if not isinstance(text, str):
        return _empty()
    return {k: list(v) if isinstance(v, list) else v
            for k, v in _parse_cached(text).items()}


@lru_cache(maxsize=PARSE_CACHE)
def _parse_cached(text: str) -> dict:
    cut = text.lstrip().startswith("…")
    sections: dict[str, list[str]] = {}
    label: Optional[str] = None
    for raw in text.splitlines():
        match = _LABEL_RE.match(raw.strip())
        if match:
            label = match.group(1)
            # A label written twice continues the first, never replaces it.
            body = sections.setdefault(label, [])
            if body and match.group(2).strip():
                body.append("")
            body.append(match.group(2))
            continue
        if label is None or _HEADING_RE.match(raw):
            continue
        sections[label].append(raw)
    if not sections:
        return _empty(cut)

    out = _empty(cut)
    out["labelled"] = True
    out["asked"] = _collapse(" ".join(_items(sections.get("Asked", []))))
    changed = _items(sections.get("Changed", []))
    verified = _items(sections.get("Verified", []))
    out["changed"], out["changed_total"] = changed[:MAX_ITEMS], len(changed)
    out["verified"], out["verified_total"] = verified[:MAX_ITEMS], len(verified)
    out["card"] = _collapse(" ".join(_items(sections.get("Card", []))))

    steps: list[str] = []
    why = ""
    for item in _items(sections.get("Unchecked", [])):
        if NOTHING_UNCHECKED_RE.match(_plain(item)):
            out["nothing_unchecked"] = True
            continue
        if WHY_NOT_RE.match(_plain(item)):
            why = _plain(item)
            continue
        if why:
            # A continuation after the reason belongs to the reason.
            why = _collapse(why + " " + item)
            continue
        steps.append(item)
    if out["nothing_unchecked"]:
        steps = []
    out["unchecked"], out["unchecked_total"] = steps[:MAX_ITEMS], len(steps)
    out["why_not_automated"] = why
    out["headline"] = headline(out)
    return out


def headline(parsed: dict) -> str:
    """The report in one line: `"Changed: <first change> · 2 verified ·
    nothing unchecked"` (or `· 3 unchecked`), whitespace collapsed and
    clamped to `HEADLINE_CHARS` with `…`. `""` for an unlabelled report.

    Leads with the first change; a report with none leads with whatever
    part it has (Asked, then the first verified check) rather than inventing
    one.
    """
    if not isinstance(parsed, dict) or not parsed.get("labelled"):
        return ""
    changed = list(parsed.get("changed") or [])
    verified = list(parsed.get("verified") or [])
    unchecked = list(parsed.get("unchecked") or [])
    parts: list[str] = []
    if changed:
        parts.append("Changed: " + _lead(str(changed[0])))
    elif parsed.get("asked"):
        parts.append("Asked: " + _lead(str(parsed["asked"])))
    elif verified:
        parts.append("Verified: " + _lead(str(verified[0])))
    if verified:
        parts.append(f"{_total(parsed, 'verified')} verified")
    if parsed.get("nothing_unchecked"):
        parts.append("nothing unchecked")
    elif unchecked:
        parts.append(f"{_total(parsed, 'unchecked')} unchecked")
    line = _collapse(" · ".join(p for p in parts if p))
    if len(line) > HEADLINE_CHARS:
        line = line[:HEADLINE_CHARS - 1].rstrip() + "…"
    return line


def _total(parsed: dict, part: str) -> int:
    """The true count of a list part — `<part>_total` where it is carried
    and no smaller than the list, else the list's own length."""
    shown = len(parsed.get(part) or [])
    try:
        total = int(parsed.get(f"{part}_total") or 0)
    except (TypeError, ValueError):
        total = 0
    return max(total, shown)


def headline_of(entry: dict) -> str:
    """The headline a published row carries, or `""` — read off the row's
    `work_report`, never recomposed from `last_report`."""
    if not isinstance(entry, dict):
        return ""
    parsed = entry.get("work_report")
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("headline") or "").strip()


__all__ = ["HEADLINE_CHARS", "LABELS", "MAX_ITEMS", "NOTHING_UNCHECKED_RE",
           "WHY_NOT_RE", "headline", "headline_of", "parse"]
