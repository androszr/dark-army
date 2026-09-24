"""Retiring the on-disk record of a background agent.

`claude agents --json` lists a background session forever. One that stopped
mid-turn to ask a question sits in `blocked` until a human answers it, and since
it has no terminal, nobody ever does — on this machine two had been parked that
way for 13 and 20 days. The daemon already files those under `abandoned` once
they pass `agents_poll.STALE_BLOCKED_SECONDS` and keeps them out of every count,
but "uncounted" is not "gone": they still list, still render, still take a slot.
The CLI has no `--kill`, `--rm`, or any other way to retire one.

The record is a directory — ``~/.claude/jobs/<short-id>/``, holding `state.json`,
a timeline and a `tmp/`. Deleting that directory is what retiring one means. The
session's *transcript* lives elsewhere (``~/.claude/projects/``) and is
deliberately left alone: this drops the job, not the work it did.

Read the header of `agents_poll.py` first — that module is emphatically read-only
and reconciliation is additive enrichment. The one destructive operation in the
reconciler's world lives here instead, so that promise keeps holding, and so the
validation guarding an `rmtree` sits in one auditable place rather than inline in
a request handler.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dark-army.jobs")

JOBS_DIR = Path.home() / ".claude" / "jobs"

# The short id names a directory, and it arrives from an HTTP request — so it is
# whitelisted, never sanitised. Real ids are 8 hex characters; the pattern is a
# little wider to survive a format change, and narrow enough that "..", "", ".",
# "a/b", "~" and an absolute path all fail it outright.
# `\Z`, not `$`: `$` also matches before a trailing newline, so "abcd1234\n"
# would pass a whitelist whose whole job is to be exact.
_SHORT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{2,63}\Z")

# Every job directory has one. Requiring it before deleting means a mistyped or
# repurposed id removes nothing: we only ever delete something we can positively
# identify as a job record.
_MARKER = "state.json"


class JobDeleteError(Exception):
    """The deletion was refused or failed. The message is shown to the user."""


def _validated_name(short_id: str) -> str:
    """The id as a bare directory name, or a refusal. Split out from `job_dir`
    because deletion has to check the name before asking whether the path
    exists — `base / ".."` "exists" for every base there is."""
    if not isinstance(short_id, str) or not _SHORT_ID.match(short_id):
        raise JobDeleteError(f"not a valid job id: {short_id!r}")
    return short_id


def job_dir(short_id: str, root: Optional[Path] = None) -> Path:
    """Validate `short_id` and return the directory it names.

    Raises JobDeleteError unless the result is, provably, a real job record
    directly inside `root`. Every check is a separate refusal so the reason
    reaches the caller intact."""
    base = (root or JOBS_DIR).resolve()
    candidate = base / _validated_name(short_id)
    # Resolve *before* comparing: a symlink in the jobs directory pointing at,
    # say, the home directory would otherwise pass a purely textual check and
    # then be handed to rmtree.
    resolved = candidate.resolve()
    if resolved.parent != base:
        raise JobDeleteError(f"job {short_id} resolves outside {base}")
    if candidate.is_symlink():
        raise JobDeleteError(f"job {short_id} is a symlink")
    if not resolved.is_dir():
        raise JobDeleteError(f"job {short_id} is not a directory")
    if not (resolved / _MARKER).is_file():
        raise JobDeleteError(f"job {short_id} has no {_MARKER}; refusing to delete")
    return resolved


def delete_job(short_id: str, root: Optional[Path] = None) -> bool:
    """Delete a job record. True if it was removed, False if it was already gone.

    Missing is not an error: the panel can be a few seconds stale, and two clicks
    racing each other should settle rather than surface a failure for work that
    is, by then, done."""
    base = (root or JOBS_DIR).resolve()
    path = base / _validated_name(short_id)
    # `is_symlink` as well as `exists`, so a dangling symlink falls through to
    # job_dir and is refused by name rather than reported as already gone.
    if not path.exists() and not path.is_symlink():
        return False

    target = job_dir(short_id, root)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        raise JobDeleteError(f"could not delete job {short_id}: {exc}") from exc
    logger.info("deleted job record %s", target)
    return True
