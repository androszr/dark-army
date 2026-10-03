"""The Worktrees list — the pure half.

One row per card side folder or branch across the enrolled projects, each with
a plain status the phone draws verbatim (`docs/card-worktrees.md`, *Several
finished cards merge one after another*). This module reads no file and runs
no git: `BobDaemon._worktrees_sync` gathers the facts and hands them here, so
the rule that sorts a card into a status is one function a test can table.
"""

from __future__ import annotations

from . import merges, worktrees

#: The statuses, in the order the rule tries them (after `no_card`).
STATUSES = ("no_card", "merging", "queued", "merged", "conflict",
            "checks_failed", "blocked", "working", "unreadable",
            "waiting", "nothing", "done")

STATUS_WORDS = {
    "no_card": "No card",
    "merging": "Merging…",
    "queued": "Waiting its turn to merge",
    "merged": "Merged",
    "conflict": "Conflict",
    "checks_failed": "Checks failed",
    "blocked": "Merge stopped",
    "working": "Work in progress",
    "unreadable": "Could not read",
    "waiting": "Finished — not ready to merge",
    "nothing": "Nothing to merge",
    "done": "Work done",
}

#: The closed key set of one row.
ROW_KEYS = ("card_id", "title", "project", "column", "branch", "folder",
            "status", "word", "line", "ahead", "uncommitted", "mergeable",
            "branch_tip")

#: How long a card that landed stays listed as Merged after its folder and
#: branch are gone (`docs/card-worktrees.md`).
MERGED_LISTED_SECONDS = 86400.0

#: Rows on one page, and the page's byte bound.
MAX_ROWS = 80
PAGE_MAX_BYTES = 300_000

NO_CARD_LINE = ("A folder under .worktrees that no card on the board names. "
                "Dark Army leaves it alone.")
RUNNING_LINE = "The card is still running."
NOTHING_LINE = "Main already has everything on this branch."

#: The statuses a person may tick (with no gate refusal and a clean folder).
MERGEABLE = ("done", "nothing", "conflict", "checks_failed", "blocked")

_RANK = {"unreadable": 2, "done": 0, "nothing": 0, "conflict": 0, "checks_failed": 0,
         "blocked": 0, "waiting": 0, "queued": 1, "merging": 1,
         "working": 2, "merged": 3, "no_card": 4}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def status_for(card, *, dirty: int = 0, ahead: int = 0, merging: bool = False,
               queued: bool = False, gate_refusal: str = "",
               merge_line: str = "", live: bool = False,
               failure: str = "") -> tuple:
    """`(status, word, line)` — the first rung that applies. `card` is None
    for a folder no card names. `dirty` is the count of uncommitted entries
    (`-1`, a failed read, counts as dirty), `ahead` the branch's commits the
    trunk lacks, `gate_refusal` the gate's words with its busy rungs left
    out, `merge_line` the card's own merge sentence."""
    if not card:
        return "no_card", STATUS_WORDS["no_card"], NO_CARD_LINE
    state = str(card.get("merge_state") or "")
    if merging:
        return "merging", STATUS_WORDS["merging"], merge_line or \
            merges.MERGING_NOTE
    if queued:
        return "queued", STATUS_WORDS["queued"], merge_line
    if state == "merged":
        return "merged", STATUS_WORDS["merged"], merge_line
    if state in ("conflict", "checks_failed", "blocked"):
        return state, STATUS_WORDS[state], merge_line
    if str(card.get("column_name") or "") != "done" or live or dirty != 0:
        if dirty > 0:
            line = _plural(dirty, "file", "files") + \
                " not committed in its folder."
        elif dirty < 0:
            line = "Git could not read this folder."
        else:
            line = RUNNING_LINE
        return "working", STATUS_WORDS["working"], line
    if failure:
        return "unreadable", STATUS_WORDS["unreadable"], failure
    if gate_refusal:
        return "waiting", STATUS_WORDS["waiting"], gate_refusal
    if ahead <= 0:
        return "nothing", STATUS_WORDS["nothing"], NOTHING_LINE
    return "done", STATUS_WORDS["done"], \
        _plural(ahead, "commit", "commits") + " ready to merge into main."


def row(*, card_id: str = "", title: str = "", project: str = "",
        column: str = "", branch: str = "", folder: str = "",
        status: str = "", word: str = "", line: str = "", ahead: int = 0,
        uncommitted: int = 0, mergeable: bool = False,
        branch_tip: str = "") -> dict:
    """One closed row. `mergeable` is only ever true for a status in
    `MERGEABLE`, a clean folder and a tip the page can echo."""
    ok = bool(mergeable) and status in MERGEABLE and int(uncommitted) == 0
    return {
        "card_id": str(card_id), "title": str(title), "project": str(project),
        "column": str(column), "branch": str(branch), "folder": str(folder),
        "status": str(status), "word": str(word), "line": str(line),
        "ahead": max(-1, int(ahead)),  # -1: not read
        "uncommitted": max(0, int(uncommitted)),
        "mergeable": ok, "branch_tip": str(branch_tip),
    }


def orphan_row(project: str, folder: str) -> dict:
    """A directory under `.worktrees/` no card names: read-only."""
    return row(project=project, folder=folder, status="no_card",
               word=STATUS_WORDS["no_card"], line=NO_CARD_LINE)


def relative_folder(name: str) -> str:
    """`.worktrees/<name>` — never an absolute path."""
    return f"{worktrees.WORKTREES_DIR}/{name}"


def order(rows) -> list:
    """Project label, then rank, then the card's `updated_at` (carried as
    the `_updated` side key when present), then card id."""
    return sorted(rows or [], key=lambda r: (
        str(r.get("project") or "").lower(),
        _RANK.get(str(r.get("status") or ""), 5),
        float(r.get("_updated") or 0.0),
        str(r.get("card_id") or ""), str(r.get("folder") or "")))
