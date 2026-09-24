"""Resolve a board card's declared specialist workflow from bounded evidence.

The workflow and the observed agent trail are deliberately different truths.
This module may fill only the former.  It never looks at sessions, transcripts,
card state, or a generic mention of a specialist and therefore cannot invent
history or promise a pipeline for unrelated work.

Plan paths are untrusted card text.  Reads are restricted to real Markdown
files below the card's own root or a project root Dark Army currently knows, and are
bounded before decoding.  Failure is no evidence, not a card-creation error.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Mapping

from .board import parse_stages
from . import areas

logger = logging.getLogger(__name__)

IMPLEMENTATION_STAGES = (
    "bc-implementer",
    "bc-verifier",
    "bc-bug-auditor",
)
PLANNING_STAGES = ("bc-planner",)

# A plan is metadata plus prose, not an input corpus.  Current plans are far
# smaller than this; rejecting a larger file keeps startup work predictable and
# makes the cap govern what is loaded rather than only what is later parsed.
MAX_PLAN_BYTES = 64 * 1024

_PLAN_LINE = re.compile(r"^Plan:\s+(.+?)\s*$")
_STAGES_LINE = re.compile(r"^(?:-\s*)?\*\*Stages:\*\*\s*(.*)$")
_IMPLEMENT_LINE = re.compile(r"^/ship\s+implement(?:\s+.+)?\s*$", re.MULTILINE)
_LEGACY_IMPLEMENT_LINE = re.compile(
    r"^Implement the accepted plan at\s+.+\s*$", re.MULTILINE)
_SHIP_LINE = re.compile(r"^/ship\s+(.+?)\s*$", re.MULTILINE)
# A stage is a short identifier-shaped token. No spaces, no backticks, no
# angle brackets, no punctuation a sentence would carry.
_STAGE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}", re.IGNORECASE)


def keep_known_stages(names: Iterable[str],
                      roster: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split stage names into those the roster declares and those it does not.

    ``(kept, dropped)``, order preserved.  Matching is case-insensitive and a
    kept name is canonicalised to the roster's own spelling, so what is stored
    and what the project's agent file is called are the same string.

    An **empty roster keeps nothing**: the caller decides whether that means
    "no evidence" or "refuse", and the two callers here answer differently.
    """
    canonical = {}
    for name in roster or ():
        text = str(name or "").strip()
        if text:
            canonical.setdefault(text.lower(), text)
    kept: list[str] = []
    dropped: list[str] = []
    for name in names or ():
        text = str(name or "").strip()
        if not text:
            continue
        match = canonical.get(text.lower())
        if match is None:
            dropped.append(text)
        else:
            kept.append(match)
    return kept, dropped


def resolve(card: Mapping, known_roots: Iterable[os.PathLike | str] = (),
            roster: Iterable[str] = ()) -> list[str]:
    """Return the specialists this card can still be expected to run.

    Precedence is explicit workflow, a leading ``Plan:`` handoff's metadata,
    the card's own ``plan_path`` (what ``attach_plan`` actually writes), then
    the two exact standard command shapes.  A generic card returns ``[]``.

    ``roster`` is the card's own project's real helper names.  When it is
    non-empty, the plan document's declared stages are filtered through
    ``keep_known_stages`` before anything else looks at them — a plan copied
    out of another repository must not stock this project's card with that
    repository's staff.  A name that survives nothing falls through to the
    narrow command default exactly as a malformed header does.  An **empty**
    roster argument means today's behaviour, unfiltered, so every existing
    caller is unchanged.
    """
    explicit = parse_stages(card.get("workflow"))
    if explicit:
        return explicit

    prompt = str(card.get("prompt") or card.get("notes") or "")
    attached = str(card.get("plan_path") or "").strip()
    # `attach_plan` writes `plan_path` and leaves `prompt` as the leftover
    # idea. That card is still implementation — the plan is on the card —
    # so the field is evidence even when the notes never grew a `Plan:` line.
    implementation = _is_implementation_handoff(prompt) or bool(attached)
    plan_path = _leading_plan_path(prompt) or attached
    if plan_path:
        declared = _read_plan_stages(
            plan_path,
            card_root=card.get("root"),
            known_roots=known_roots,
        )
        if declared:
            if roster:
                declared, unknown = keep_known_stages(declared, roster)
                if unknown:
                    logger.debug(
                        "plan header names helpers this project does not "
                        "have: %r", unknown)
            if implementation:
                declared = [name for name in declared if name != "bc-planner"]
            if declared:
                return declared

    if implementation:
        return list(IMPLEMENTATION_STAGES)
    if _is_planning_handoff(prompt):
        return list(PLANNING_STAGES)
    return []


def _leading_plan_path(prompt: str) -> str:
    """The first non-empty line's exact ``Plan:`` value, if it is Markdown."""
    for line in prompt.splitlines():
        if not line.strip():
            continue
        match = _PLAN_LINE.fullmatch(line)
        if not match:
            return ""
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
            value = value[1:-1].strip()
        return value if Path(value).suffix.lower() == ".md" else ""
    return ""


def _is_implementation_handoff(prompt: str) -> bool:
    # A leading Plan handoff is the shape /ship files for implementation.  The
    # command may also arrive directly in a dispatched/manual card.
    return bool(
        _leading_plan_path(prompt)
        or _IMPLEMENT_LINE.search(prompt)
        or _LEGACY_IMPLEMENT_LINE.search(prompt)
    )


def _is_planning_handoff(prompt: str) -> bool:
    for match in _SHIP_LINE.finditer(prompt):
        idea = match.group(1).strip()
        if idea and not re.match(r"^implement(?:\s|$)", idea):
            return True
    return False


def _expanded(raw) -> Path | None:
    """``Path(raw).expanduser()`` that answers None instead of raising.

    A card's plan path is untrusted text and ``expanduser`` is not total: an
    unknown ``~user`` raises ``RuntimeError``, and this module's contract is
    that a bad path is no evidence rather than an error. Left to raise, one
    poisoned card took the whole board down at startup.
    """
    try:
        return Path(str(raw)).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def _allowed_roots(card_root, known_roots: Iterable[os.PathLike | str]) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for raw in (card_root, *(known_roots or ())):
        if not raw:
            continue
        expanded = _expanded(raw)
        if expanded is None:
            continue
        try:
            root = expanded.resolve(strict=True)
            if not root.is_dir() or root in seen:
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _contained(candidate: Path, roots: list[Path]) -> bool:
    return any(candidate == root or root in candidate.parents for root in roots)


def _candidate_paths(raw_path: str, card_roots: list[Path]) -> list[Path]:
    path = _expanded(raw_path)
    if path is None:
        return []
    if path.is_absolute():
        return [path]
    # A relative path belongs to the card that named it. Trying the same text
    # under every open project lets a missing A/plans/work.md silently read
    # B/plans/work.md and persist B's workflow onto A's card.
    return [root / path for root in card_roots]


def resolve_plan_file(raw_path: str, card_root, known_roots) -> Path | None:
    """The one readable, contained, in-bounds plan file this text names.

    Split out of `_read_plan_stages` so the *containment* half — which is the
    only interesting half — has exactly one implementation, shared by the
    workflow reader and by `declared_files`. Two copies of a path-containment
    test is two chances to get it wrong, and this one is reached from a
    channel tool anything on the machine can call.

    Returns the resolved path (never the text), so a caller that wants to
    cache on `(path, mtime_ns, size)` can stat it without re-resolving.
    """
    roots = _allowed_roots(card_root, known_roots)
    if not roots:
        return None
    card_roots = _allowed_roots(card_root, ())
    # Lexically relative input stays owned by the card even after resolving
    # ``..``. Without the narrower final check, A/../B/plans/work.md becomes an
    # absolute path under known root B and silently imports B's workflow.
    path = _expanded(raw_path)
    if path is None:
        return None
    containment_roots = roots if path.is_absolute() else card_roots
    for unresolved in _candidate_paths(raw_path, card_roots):
        try:
            candidate = unresolved.resolve(strict=True)
            if (candidate.suffix.lower() != ".md"
                    or not _contained(candidate, containment_roots)):
                continue
            stat = candidate.stat()
            if not candidate.is_file() or stat.st_size > MAX_PLAN_BYTES:
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        return candidate
    return None


def read_plan_text(path: Path) -> str:
    """A resolved plan's text, bounded. `""` on any failure — this module's
    standing rule that failure is no evidence rather than an error."""
    try:
        with Path(path).open("rb") as handle:
            payload = handle.read(MAX_PLAN_BYTES + 1)
        if len(payload) > MAX_PLAN_BYTES:
            return ""
        return payload.decode("utf-8")
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return ""


def _read_plan_stages(raw_path: str, card_root, known_roots) -> list[str]:
    candidate = resolve_plan_file(raw_path, card_root, known_roots)
    if candidate is None:
        return []
    text = read_plan_text(candidate)
    if not text:
        return []
    return _parse_header_stages(text)


#: How many rows one table may contribute. `PlanStructure.parseFiles`' own cap,
#: kept identical because the two parsers are pinned to one another by a
#: contract test: a plan whose table the panel truncates and the daemon does
#: not would draw one set of files and parse another.
MAX_TABLE_ROWS = 24

#: The two tables a plan uses to say what it will touch. `## New files` is
#: included because a plan that only *creates* files is still saying what it
#: will touch. The panel's `PlanStructure.parseFiles` deliberately reads only
#: `## Files to change` — it breaks at the next `## ` heading, because the
#: diagram is about what the plan will change. What is pinned across the two
#: is `parse_declared_files`' cell-level rule, not the set of sections.
_FILE_SECTIONS = ("## files to change", "## new files")


def declared_files(card: Mapping,
                   known_roots: Iterable[os.PathLike | str] = ()) -> list[str]:
    """The files this card's plan says it will touch. `[]` means *unknown*.

    **No decision has consulted this since 5 Sep 2026**, when the file-overlap
    gate was removed; it is kept for `parse_declared_files`' contract with the
    panel's `PlanStructure.parseFiles`, which draws the same table on the card.

    `[]` is deliberately not "touches nothing": a card with no `plan_path`, a
    plan that has been deleted, a plan too large to read, and a plan whose
    tables are empty or malformed all collapse to the same *unknown* answer —
    which is why nothing here reports *why* it found nothing. Read live and
    never stored on the card, so an edited plan changes the answer on the next
    read with no invalidation anywhere.
    """
    raw = str((card or {}).get("plan_path") or "").strip()
    if not raw:
        return []
    candidate = resolve_plan_file(raw, (card or {}).get("root"), known_roots)
    if candidate is None:
        return []
    return parse_declared_files(read_plan_text(candidate))


def parse_declared_files(text: str) -> list[str]:
    """First cells of the plan's file tables, from a plan's already-read text.

    Line for line `PlanStructure.parseFiles`' rules — a `## ` heading opens or
    closes a section, only `|`-leading lines inside one count, the header row
    and the `---` separator row are dropped, backticks are stripped, and each
    table is capped at `MAX_TABLE_ROWS`. The two implementations are pinned to
    one another by a contract test over one literal table, because a
    disagreement here means the picture on the card and this parser are
    reading different plans.
    """
    in_section = False
    rows = 0
    out: list[str] = []
    for raw_line in str(text or "").split("\n"):
        line = raw_line.strip()
        if line.startswith("## "):
            was = in_section
            in_section = line.lower().startswith(_FILE_SECTIONS)
            if in_section and not was:
                rows = 0
            continue
        if not in_section or not line.startswith("|"):
            continue
        cell = _first_cell(line)
        if cell is None:
            continue
        if cell not in out:
            out.append(cell)
        rows += 1
        if rows >= MAX_TABLE_ROWS:
            in_section = False
    return out


def _first_cell(row: str) -> str | None:
    """A table row's first cell, or None for the header and separator rows."""
    cells = [part.strip() for part in str(row).split("|")]
    cells = [part for part in cells if part]
    if not cells:
        return None
    first = cells[0]
    # The separator row is dashes and colons; the header row is the template's
    # own literal label. Neither is a file.
    if all(ch in "-: " for ch in first):
        return None
    if first.casefold() == "file":
        return None
    return first.strip("`").strip()


def _parse_header_stages(text: str) -> list[str]:
    """Parse only metadata before the first H2, never prose with that phrase."""
    header_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        header_lines.append(line)
    for index, line in enumerate(header_lines):
        match = _STAGES_LINE.fullmatch(line.strip())
        if not match:
            continue
        value_parts = [match.group(1).strip()]
        # Metadata guidance can wrap on indented continuation lines.  Stop at a
        # blank, another metadata bullet, or any heading.
        for continuation in header_lines[index + 1:]:
            if not continuation.strip() or continuation.lstrip().startswith(("- **", "#")):
                break
            if continuation[:1].isspace():
                value_parts.append(continuation.strip())
                continue
            break
        raw = " ".join(part for part in value_parts if part)
        names = [_strip_aside(part) for part in re.split(r"[|,]", raw)]
        names = [name for name in names if name]
        if not names or not all(_header_stage_ok(name) for name in names):
            # Malformed means *no evidence*, all-or-nothing, not a filtered
            # subset: this repo's own template ships the header with a sentence
            # of guidance in it, and splitting that sentence on `[|,]` yields
            # six plausible-looking fragments that would then be drawn as
            # hollow "still to come" markers. One bad name means the header is
            # prose, so the whole header is dropped and the caller falls
            # through to the narrow command default.
            #
            # Said out loud, or a rejected header and an absent one look the
            # same from outside: both draw the generic track, and the author
            # of the header has nothing to tell them which happened.
            logger.debug("plan header rejected as prose: %r", raw)
            return []
        return parse_stages(names)
    return []


def _strip_aside(part: str) -> str:
    """One element of a `Stages:` header, with a trailing aside removed.

    The template invites a parenthetical — "add `bc-integration-reviewer` only
    when this plan requires that audit" — so an author plausibly writes
    `bc-integration-reviewer (this plan touches the API surface)`. Validation
    is all-or-nothing, so without this the aside fails `_header_stage_ok`, the
    whole header is dropped, and the fallback is the same list *minus* the
    reviewer: a track that looks right and is one declared stage short. Only a
    *trailing* aside is cut, and only around the name — anything else still
    reads as prose and still drops the header.
    """
    text = str(part or "").strip().strip("`").strip()
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text).strip()
    return text.strip("`").strip()


def _header_stage_ok(name: str) -> bool:
    """Whether a name read out of a plan header is a stage and not prose.

    `BobDaemon._stage_name_ok` is the model: conservative in one direction
    only. A real stage named unusually is dropped, which is a track short by
    one; a fragment of the template's own guidance *drawn* is a track that is
    wrong.
    """
    return bool(_STAGE_NAME.fullmatch(str(name or "").strip()))


_AREA_LINE = re.compile(r"^(?:-\s*)?\*\*Area:\*\*\s*(.*)$")


def parse_header_area(text: str) -> str:
    """First explicit Area header; invalid metadata supplies no suggestion."""
    for line in (text or "").splitlines():
        match = _AREA_LINE.match(line.strip())
        if match:
            return areas.normalise(match.group(1))[0]
    return ""


def read_plan_area(path) -> str:
    return parse_header_area(read_plan_text(path))


#: The plan's objective, as three header lines beside `Area:` — the same
#: `- **Label:** value` shape, matched on the label alone so a renderer's
#: bold is optional and a stray `Area: desk` in prose still is not a header.
#: Keyed by the card's own objective field, so the seed needs no renaming.
_OBJECTIVE_LINES = {
    "beneficiary": re.compile(r"^(?:-\s*)?\*\*Who benefits:\*\*\s*(.*)$"),
    "intended_benefit": re.compile(r"^(?:-\s*)?\*\*Intended benefit:\*\*\s*(.*)$"),
    "success_criterion": re.compile(r"^(?:-\s*)?\*\*Success criterion:\*\*\s*(.*)$"),
}

#: A template placeholder or an explicit "none" is no objective, not one
#: that says `<who this is for>` — the planner brief allows `NONE` where the
#: person stated nothing, and the template's angle-bracket hint must never
#: land on a card.
_OBJECTIVE_NONE = ("", "none", "n/a", "-", "—")


def parse_header_objective(text: str) -> dict:
    """The first explicit objective header of each kind, `{field: words}`,
    with only the fields the plan states. A placeholder (`<...>`), `NONE`
    or a blank value supplies nothing for that field."""
    found: dict = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        for field, pattern in _OBJECTIVE_LINES.items():
            if field in found:
                continue
            match = pattern.match(stripped)
            if not match:
                continue
            value = match.group(1).strip()
            if value.lower() in _OBJECTIVE_NONE or (
                    value.startswith("<") and value.endswith(">")):
                continue
            found[field] = value
    return found


def read_plan_objective(path) -> dict:
    return parse_header_objective(read_plan_text(path))
