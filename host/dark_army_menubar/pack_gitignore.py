# host/dark_army_menubar/pack_gitignore.py
"""The starter ignore list the agent pack offers a project, merged in memory.

Pure and stdlib-only: text in, text out. The installer (``pack_install``)
reads the project's ``.gitignore``, hands its text here with the lines the
pack ships and the lines it has already offered that project, and writes
back what comes out. Nothing here opens a file.

Three rules:

- **Equivalent lines are the same line.** ``/plans/``, ``plans``,
  ``plans/`` and ``**/plans/`` all mean "ignore a thing called plans" for
  the purpose of "is it already there" (``canonical``).
- **A negation is never skipped as already present.** Order matters in a
  gitignore: ``!.env.example`` only works *after* the ``.env.*`` it
  excepts, so the pack's copy is appended even when the project already has
  one above. A plain line is skipped when the project deliberately
  un-ignored it or anything it would match (``!.env`` keeps ``.env`` out,
  ``!.env.production`` keeps ``.env.*`` out): git obeys the last matching
  line, so appending it after the negation would hide that file again.
- **Offered once.** A line whose canonical form the project has already
  been offered is never appended again, so a line somebody deleted from the
  block stays deleted.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

HEADER = "# managed by Dark Army"


def canonical(line: str) -> str:
    """The form two lines are compared in. ``""`` for a blank or a comment."""
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    negated = text.startswith("!")
    body = text[1:] if negated else text
    if body.startswith("**/"):
        body = body[3:]
    if body.startswith("/"):
        body = body[1:]
    if body.endswith("/"):
        body = body[:-1]
    if not body:
        return ""
    return "!" + body if negated else body


def _overrides_a_negation(form: str, negated: set[str]) -> bool:
    """Would appending ``form`` re-ignore something a ``!`` line un-ignored?

    ``negated`` holds the bodies of the project's ``!`` lines in canonical
    form. A body is compared whole and by its last path segment, since a
    pattern without a slash matches at any depth.
    """
    for body in negated:
        if body == form:
            return True
        if fnmatchcase(body, form) or fnmatchcase(body.rsplit("/", 1)[-1], form):
            return True
    return False


def merge(
    existing: str,
    offered: list[str],
    already_offered: set[str],
) -> tuple[str, list[str], list[str]]:
    """Append the offered lines ``existing`` lacks, under ``HEADER``.

    Returns ``(new_text, appended, offered_now)``: the file's new text (the
    very same string when nothing is appended), the raw lines appended in
    offered order, and the canonical form of every offered line — appended
    or skipped — which is what the caller records as offered.
    """
    present = {canonical(line) for line in existing.splitlines()} - {""}
    # A project's un-ignore the pack re-appends after its own block's
    # patterns stays in force, so only the others can be overridden.
    restored = {canonical(raw)[1:] for raw in offered
                if canonical(raw).startswith("!")
                and canonical(raw) not in already_offered}
    negated = {form[1:] for form in present if form.startswith("!")} - restored
    appended: list[str] = []
    taken: set[str] = set()
    offered_now: list[str] = []
    for raw in offered:
        line = str(raw).strip()
        form = canonical(line)
        if not form:
            continue
        if form not in offered_now:
            offered_now.append(form)
        if form in already_offered or form in taken:
            continue
        if not form.startswith("!") and (
                form in present or _overrides_a_negation(form, negated)):
            continue
        appended.append(line)
        taken.add(form)
    if not appended:
        return existing, [], offered_now
    lines = existing.splitlines(keepends=True)
    header_at = next(
        (i for i, line in enumerate(lines) if line.strip() == HEADER), None)
    if header_at is not None:
        end = header_at + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        before = lines[:end]
        if not before[-1].endswith("\n"):
            before[-1] += "\n"
        added = "".join(line + "\n" for line in appended)
        return "".join(before) + added + "".join(lines[end:]), appended, offered_now
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    text += HEADER + "\n" + "".join(line + "\n" for line in appended)
    return text, appended, offered_now
