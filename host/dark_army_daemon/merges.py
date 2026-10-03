"""Reviewing and merging a Done card's branch — the pure half.

A Done card keeps the memory of its branch (`docs/card-worktrees.md`, *Review
and merge*). This module holds every *decision* about landing it on the local
main line: the git argv, the parsers, every bound and the sentences both
clients draw verbatim. It runs no subprocess, opens no file and imports
nothing from the daemon but `work_record` and `worktrees`, whose `_git` head
every argv here is built on — so `--no-optional-locks` and `core.quotePath=false`
ride every call. **The git argv live in three pure modules, `work_record.py`,
`worktrees.py` and this one; every call runs through `BobDaemon._run_git`.**

The shape of a merge, so the argv below read in order: the merge commit is
built in the card's *own* folder, detached at the trunk's tip, so the person's
main checkout is never mid-anything; the project's check script runs there;
and only then does the trunk move, by a fast-forward onto that very commit
(or a guarded `update-ref` where the trunk is checked out nowhere). Nothing
here pushes, and nothing here discards: the branch is deleted with the safe
form only, after the merge.
"""

from __future__ import annotations

import os
import re

from . import work_record, worktrees

#: The main line when `origin/HEAD` names none and a local `main` exists.
TRUNK_DEFAULT = "main"
#: The merge commit and the guarded move of the trunk. A conflict-free merge
#: of a large branch takes seconds; the fast-forward checks files out.
MERGE_TIMEOUT_SECONDS = 120.0
FF_TIMEOUT_SECONDS = 60.0
#: The person's optional check script, relative to the root, under the setup
#: script's rules (`docs/card-worktrees.md`, *The setup script*).
MERGE_CHECK_SCRIPT = ".dark-army/merge-check.sh"
#: The checks' whole budget (a project's test suite).
MERGE_CHECK_TIMEOUT_SECONDS = 1800.0
#: How much of the check script's output the log keeps.
MAX_MERGE_LOG_BYTES = 256_000
#: Paths named in one sentence before "and N more".
MAX_LISTED_PATHS = 8
#: Commits the Changes view lists.
MAX_COMMITS = 100
#: The card's title in the merge commit's subject, in characters.
MAX_TITLE_CHARS = 72

#: What `merge_state` can hold. `""` is "nothing has happened"; `merging` is
#: never stored — the snapshot composes it from the running set.
STATES = ("", "blocked", "conflict", "checks_failed", "merged")
#: The two states the Fix press answers.
FIX_STATES = ("conflict", "checks_failed")
SNAPSHOT_MERGING = "merging"
#: Snapshot-only like `merging`: a card waiting its turn in a batch. Never
#: stored, never in `STATES`.
SNAPSHOT_QUEUED = "queued"
#: What `review_verdict` can hold.
VERDICTS = ("", "ship", "stop")

#: The files outside the work tree that mean the root is busy with something.
ROOT_BUSY_MARKERS = ("MERGE_HEAD", "rebase-merge", "rebase-apply",
                     "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
#: What a folder must not hold before a merge is built in it.
MID_MERGE_MARKERS = ("MERGE_HEAD", "rebase-merge", "rebase-apply")

#: Every key the Changes read carries, in a closed set.
CHANGES_KEYS = (
    "available", "card_id", "branch", "trunk", "merge_base", "branch_tip",
    "ahead", "behind", "commits", "files", "files_total", "files_truncated",
    "commits_truncated", "merge_offered", "merge_refusal", "merge_state",
    "merge_note", "review", "generated_at", "reason")
COMMIT_KEYS = ("sha8", "author", "at", "subject")
FILE_ROW_KEYS = ("path", "added", "removed", "binary")
DIFF_KEYS = ("available", "path", "text", "truncated", "reason")
REVIEW_KEYS = ("verdict", "tip", "current")

_TIP = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_VERDICT = re.compile(r"^\s*VERDICT:\s*(SHIP|STOP)\b", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


# --- the sentences -----------------------------------------------------------

#: What the card says while a merge runs, and the press's reply.
MERGING_NOTE = "Merging this card's branch into main…"
#: `.format(trunk, files)` — the files named, clamped (`listed`).
CONFLICT_NOTE = ("Merge needs you: merging {0} into this card's branch stopped "
                 "on conflicts in {1}.")
#: `.format(code, log)` — the exit code or "timed out", and the log's path.
CHECKS_FAILED_NOTE = ("Merge needs you: the project's merge checks failed "
                      "({0}) — see {1}.")
#: The fix helper is in the folder / is not.
HELPER_AT_WORK_SUFFIX = " An assistant is working on it in the card's folder."
PRESS_AGAIN_SUFFIX = (" Press Fix to open this card's assistant in its "
                      "folder, or fix it by hand and press MERGE again.")
#: `.format(trunk, sha8)`.
MERGED_NOTE = "Merged into {0} as {1}."
MERGED_UNCHECKED_NOTE = " No checks ran: the project has no merge check script."
ALREADY_MERGED_NOTE = ("This card's branch was already part of the main line, "
                       "so nothing new was merged.")
BRANCH_KEPT_SUFFIX = " The branch was kept."
#: Neutral on purpose: the release keeps a folder for several reasons (work
#: not committed, crew output git ignores, a batch sibling at work, a session
#: still inside) and the merge does not know which.
FOLDER_KEPT_SUFFIX = (" Dark Army kept the card's folder because something in "
                      "it is not committed or is still in use.")
REVIEW_RUNNING_LINE = "Review running…"
#: `.format("SHIP" | "STOP")`.
REVIEW_LINE = "Review: {0}"
REVIEW_STALE_SUFFIX = " — reviewed at an earlier version"

# One refusal per way a press can be turned away. Each is plain words a
# person reads on the card; `.format` fields are named where there are any.
NOT_DONE_REFUSAL = "Only a card in Done can be reviewed or merged."
NO_BRANCH_REFUSAL = ("Dark Army has no branch on record for this card, so "
                     "there is nothing to merge.")
NO_CARD_REFUSAL = "Dark Army has no such card."
BOARD_CLOSED_REFUSAL = "The board is not open."
ALREADY_MERGED_REFUSAL = "This card is already merged into the main line."
MANUAL_OPEN_REFUSAL = ("This card's hand-check is still open — record it as "
                       "Passed or mark it checked before merging.")
MANUAL_FAILED_REFUSAL = ("This card's hand-check was recorded as Failed — "
                         "reopen it and check it again before merging.")
NOT_ENROLLED_REFUSAL = ("This project is no longer one Dark Army watches, so "
                        "it will not touch its git.")
NOT_A_CHECKOUT_REFUSAL = "This project's folder is not a git checkout."
CARD_LIVE_REFUSAL = ("This card's assistant is still running — wait for it to "
                     "finish before merging.")
SESSION_INSIDE_REFUSAL = ("A session is still open in this card's folder — "
                          "close it before merging.")
RELEASE_PENDING_REFUSAL = ("Dark Army is still tidying this card's folder — "
                           "press again in a moment.")
MERGE_RUNNING_REFUSAL = "This card is already merging."
PROJECT_MERGING_REFUSAL = ("Another card of this project is merging — press "
                           "again when it has finished.")
TIP_CHANGED_REFUSAL = ("This card's branch changed since you looked — open "
                       "its changes again and press MERGE once more.")
SHARED_BRANCH_REFUSAL = "Shares its branch with {0}, which merges it."
MERGE_QUEUED_NOTE = "Waiting its turn to merge — {0} of {1} in this batch."
MERGE_QUEUED_REFUSAL = ("This card is waiting its turn in a batch of merges — "
                        "press again when the batch has finished.")
BATCH_RUNNING_REFUSAL = ("A batch of merges is already running — press "
                         "again when it has finished.")
BATCH_TOO_MANY_REFUSAL = "Merge at most 8 cards at a time."
BATCH_EMPTY_REFUSAL = "No cards were chosen to merge."
#: How long a batch waits for another merge of the project: above the
#: merge, check and fast-forward bounds together.
BATCH_WAIT_SECONDS = 2400.0
REVIEW_RUNNING_REFUSAL = "A review of this card is already running."
PREPARING_REFUSAL = ("Dark Army is preparing this card's folder — press "
                     "again in a moment.")
DISPATCH_OFF_REFUSAL = ("Starting assistants is switched off, so Dark Army "
                        "will not start one for this card.")
FOLDER_FAILED_REFUSAL = ("Dark Army could not make this card's folder ready "
                         "for the merge: {0}")
MID_MERGE_REFUSAL = ("This card's folder is in the middle of a merge or a "
                     "rebase — finish or abort it by hand, then press MERGE "
                     "again.")
DIRTY_FOLDER_REFUSAL = ("This card's folder has changes that were not "
                        "committed — commit or remove them, then press MERGE "
                        "again.")
NO_TRUNK_REFUSAL = ("Dark Army could not find the project's main line "
                    "(neither origin's default branch nor a local main).")
TRUNK_MOVED_REFUSAL = ("{0} moved while the merge was being built, so Dark "
                       "Army changed nothing — press MERGE again.")
ROOT_BUSY_REFUSAL = ("The project's main checkout is in the middle of a "
                     "merge, rebase or similar — finish it, then press MERGE "
                     "again. Nothing was changed.")
TRUNK_ELSEWHERE_REFUSAL = ("{0} is checked out in another folder ({1}), so "
                           "Dark Army will not move it from here — nothing "
                           "was changed.")
OVERLAP_REFUSAL = ("The merge would change files you have not saved in the "
                   "main checkout: {0}. Commit or stash them, then press "
                   "MERGE again. Nothing was changed.")
FF_FAILED_REFUSAL = ("Git would not move {0} forward ({1}) — nothing was "
                     "changed. Press MERGE again.")
MERGE_FAILED_REFUSAL = ("Git could not build the merge ({0}) — nothing was "
                        "changed on {1}.")
GIT_FAILED_REFUSAL = "Git could not answer ({0}), so Dark Army changed nothing."
FIX_NOT_NEEDED_REFUSAL = ("This card's merge does not need fixing — there was "
                          "no conflict or failed check.")
HELPER_TOOL_MISSING_REFUSAL = ("Dark Army could not find the {0} program to "
                               "start an assistant in this card's folder.")
CHANGES_MOVED_REFUSAL = ("This card's branch changed since the list was "
                         "read — open its changes again.")
NO_SUCH_FILE_REFUSAL = "The change list has no such file."
MOVE_WHILE_MERGING_REFUSAL = ("This card is being merged right now — wait "
                              "for the merge to finish before moving it.")
START_WHILE_MERGING_REFUSAL = ("This card is being merged right now — wait "
                               "for the merge to finish before starting it.")
#: The folder could not be put back on its branch after a stop. No Fix is
#: offered (the state is `blocked`): an assistant opened on a detached folder
#: would commit onto nothing.
DETACHED_NOTE = ("The merge stopped, and Dark Army could not put this card's "
                 "folder back on its branch — it is left detached at the "
                 "merge commit. Check it by hand ({0}), then press MERGE "
                 "again.")
ROOT_UNSURE_REFUSAL = ("Dark Army could not tell which branch the main "
                       "checkout is on, so it changed nothing — press MERGE "
                       "again.")
BAD_REQUEST_REFUSAL = "That request is not one Dark Army understands."


# --- the names ----------------------------------------------------------------

def merge_log_path(root: str, card_id) -> str:
    """`<root>/.worktrees/card-<id8>.merge.log` — beside the setup log, inside
    the excluded folder."""
    return os.path.join(str(root), worktrees.WORKTREES_DIR,
                        f"{worktrees.FOLDER_PREFIX}"
                        f"{worktrees.short_id(card_id)}.merge.log")


def merge_subject(card) -> str:
    """`Merge card/<id8>: <title>` — the subject the history's own merge
    commits carry. The title is whitespace-collapsed and clamped; an empty
    one leaves the id alone."""
    card = card if isinstance(card, dict) else {}
    title = _SPACES.sub(" ", str(card.get("title") or "")).strip()
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    head = f"Merge {worktrees.BRANCH_PREFIX}{worktrees.short_id(card.get('id'))}"
    return f"{head}: {title}" if title else head


def _clamp_title(title) -> str:
    title = _SPACES.sub(" ", str(title or "")).strip()
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title


def batch_report(started, skipped) -> str:
    """The batch press's reply: what is merging, in order, and what was not
    taken and why. `started` is a list of titles, `skipped` a list of
    `(title, words)`."""
    started = list(started or [])
    skipped = list(skipped or [])
    left = "; ".join(f"{_clamp_title(t)} — {str(w).rstrip('.')}"
                     for t, w in skipped)
    if not started:
        return f"Nothing was merged: {left}." if left else "Nothing was merged."
    noun = "card" if len(started) == 1 else "cards"
    text = (f"Merging {len(started)} {noun} into main, one after another: "
            + ", ".join(_clamp_title(t) for t in started) + ".")
    if left:
        text += f" Not merged: {left}."
    return text


def merge_env(base=None) -> dict:
    """`work_record.git_env` plus the two settings that keep a merge from
    ever opening an editor."""
    env = work_record.git_env(base)
    env["GIT_EDITOR"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    return env


# --- the argv ----------------------------------------------------------------

def argv_head_branch(root: str) -> list:
    """The branch `HEAD` is on, short; exit 1 and nothing when detached."""
    return work_record._git(root) + ["symbolic-ref", "-q", "--short", "HEAD"]


def argv_tip(root: str, ref: str) -> list:
    """The commit a ref names (full hash), or nothing."""
    return work_record._git(root) + [
        "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]


def argv_merge_base(root: str, left: str, right: str) -> list:
    return work_record._git(root) + ["merge-base", str(left), str(right)]


def argv_ahead_behind(root: str, trunk: str, branch: str) -> list:
    """`<behind>\\t<ahead>`: commits only the trunk has, then commits only
    the branch has."""
    return work_record._git(root) + [
        "rev-list", "--left-right", "--count", f"{trunk}...{branch}"]


def argv_log(root: str, base: str, branch: str) -> list:
    """The branch's own commits since `base`, newest first, one more than the
    page holds so a cut is knowable."""
    return work_record._git(root) + [
        "log", "--no-color", "--format=%H%x1f%an%x1f%at%x1f%s%x1e",
        "-n", str(MAX_COMMITS + 1), f"{base}..{branch}", "--"]


def argv_numstat_range(root: str, base: str, branch: str) -> list:
    """Files the branch changed since `base`, with line counts, NUL
    separated. No rename detection: a rename is a delete and an add, so a
    row's path is always a path that exists on one side."""
    return work_record._git(root) + [
        "diff", "--numstat", "-z", "--no-renames", str(base), str(branch), "--"]


def argv_range_file_diff(root: str, base: str, branch: str, rel: str) -> list:
    """One file's changes since `base`, the path taken literally."""
    return work_record._git(root) + [
        "--literal-pathspecs", "diff", "--no-color", "--no-renames",
        str(base), str(branch), "--", str(rel)]


def argv_checkout_detach(path: str, ref: str) -> list:
    """Put the folder on a bare commit — the trunk's tip — so the merge
    commit hangs from no branch."""
    return work_record._git(path) + ["checkout", "--quiet", "--detach", str(ref)]


def argv_checkout_branch(path: str, branch: str) -> list:
    """Put the folder back on the card's branch."""
    return work_record._git(path) + ["checkout", "--quiet", str(branch), "--"]


def argv_merge(path: str, branch: str, subject: str) -> list:
    """The merge commit: always a commit (`--no-ff`), titled `subject`."""
    return work_record._git(path) + [
        "merge", "--no-ff", "--quiet", "-m", str(subject), str(branch)]


def argv_merge_abort(path: str) -> list:
    return work_record._git(path) + ["merge", "--abort"]


def argv_unmerged(path: str) -> list:
    """The files a stopped merge left in conflict, NUL separated."""
    return work_record._git(path) + [
        "diff", "--name-only", "-z", "--diff-filter=U"]


def argv_status(path: str) -> list:
    """Everything uncommitted in a folder: modified, staged, every untracked
    file listed, NUL separated."""
    return work_record._git(path) + [
        "status", "--porcelain", "-z", "--untracked-files=all"]


def argv_incoming(root: str, base: str, tip: str) -> list:
    """The paths a move from `base` to `tip` changes in a work tree, NUL
    separated, renames as a delete and an add."""
    return work_record._git(root) + [
        "diff", "--name-only", "-z", "--no-renames", str(base), str(tip), "--"]


def argv_ff(root: str, tip: str) -> list:
    """Move the checked-out branch forward onto `tip` — and only forward."""
    return work_record._git(root) + ["merge", "--ff-only", "--no-edit", str(tip)]


def argv_update_ref(root: str, trunk: str, new: str, old: str) -> list:
    """Move the branch pointer, only if it still holds `old`."""
    return work_record._git(root) + [
        "update-ref", "-m", "Dark Army: merge a finished card",
        f"refs/heads/{trunk}", str(new), str(old)]


def argv_branch_delete(root: str, branch: str) -> list:
    """The safe delete: git refuses a branch that is not merged."""
    return work_record._git(root) + ["branch", "-d", str(branch)]


def argv_branch_delete_ref(root: str, branch: str, old: str) -> list:
    """Delete a branch's ref only while it still holds `old` (the tip that
    was merged). Not `branch -D`, and not `-d` either, which judges the
    branch against whatever the main checkout has checked out and so
    refuses when that is not the trunk."""
    return work_record._git(root) + [
        "update-ref", "-m", "Dark Army: delete a merged card branch", "-d",
        f"refs/heads/{branch}", str(old)]


def argv_ls_files_tagged(path: str, paths) -> list:
    """Which of `paths` the folder's index holds, each with its state tag
    (`H` cached, `S` skip-worktree, …), NUL separated (`parse_tagged`)."""
    return work_record._git(path) + [
        "--literal-pathspecs", "ls-files", "-t", "-z", "--",
        *[str(p) for p in paths]]


def argv_restore_head(path: str, paths) -> list:
    """Put paths back to the folder's `HEAD` bytes, index and file both."""
    return work_record._git(path) + [
        "--literal-pathspecs", "checkout", "HEAD", "--",
        *[str(p) for p in paths]]


def argv_git_path(path: str, *names: str) -> list:
    """Where git keeps each of `names` for this work tree (correct when
    `.git` is a file), one line per name, in order."""
    argv = work_record._git(path) + ["rev-parse"]
    for name in names:
        argv += ["--git-path", str(name)]
    return argv


# --- the parsers --------------------------------------------------------------

def _text(data) -> str:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return str(data or "")


def trunk_from(symbolic) -> str:
    """The trunk's local name out of `argv_base_ref`'s answer (`origin/main`
    → `main`), or `""`."""
    base = worktrees.base_from(_text(symbolic).strip())
    if not base:
        return ""
    return base.split("/", 1)[1] if base.startswith("origin/") else base


def is_tip(value) -> bool:
    """Whether `value` is a full commit hash, lowercase."""
    return isinstance(value, str) and bool(_TIP.match(value))


def parse_paths_z(data) -> list:
    """NUL-separated paths into a list; empties dropped, a quoted path
    dropped for `work_record.parse_numstat`'s reason."""
    out = []
    for chunk in _text(data).split("\0"):
        path = chunk.strip("\n")
        if path and not path.startswith('"'):
            out.append(path)
    return out


def parse_tagged(data) -> dict:
    """`argv_ls_files_tagged`'s answer as `{path: {tags}}`."""
    out: dict = {}
    for entry in _text(data).split("\0"):
        if len(entry) > 2 and entry[1] == " ":
            out.setdefault(entry[2:], set()).add(entry[0])
    return out


def parse_status_paths(data) -> list:
    """The paths `argv_status` lists, both sides of a rename or a copy.
    Ignored entries are not in the output at all."""
    entries = _text(data).split("\0")
    out = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        code, path = entry[:2], entry[3:]
        if code == "!!":
            continue
        out.append(path)
        if ("R" in code or "C" in code) and i < len(entries) and entries[i]:
            out.append(entries[i])
            i += 1
    return out


def overlap(dirty, incoming) -> list:
    """The sorted unsaved paths the move would touch: one equal to a path
    the move changes, or one that is a folder holding it or inside it (a file
    where a folder would be made, or the reverse)."""
    incoming = sorted({str(p) for p in incoming or [] if p})
    hit = set()
    for d in sorted({str(p) for p in dirty or [] if p}):
        for i in incoming:
            if d == i or i.startswith(d + "/") or d.startswith(i + "/"):
                hit.add(d)
                break
    return sorted(hit)


def listed(paths, limit: int = MAX_LISTED_PATHS) -> str:
    """Paths in one sentence: the first few, then "and N more"."""
    paths = [str(p) for p in paths or []]
    if len(paths) <= limit:
        return ", ".join(paths)
    return f"{', '.join(paths[:limit])} and {len(paths) - limit} more"


def parse_worktree_branches(text) -> dict:
    """`{realpath: branch}` over `git worktree list --porcelain`; a detached
    or bare entry maps to `""`. Read by name, never counted."""
    out: dict = {}
    path = None
    for line in _text(text).splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree "):].strip())
            out[path] = ""
        elif line.startswith("branch ") and path is not None:
            ref = line[len("branch "):].strip()
            out[path] = ref[len("refs/heads/"):] \
                if ref.startswith("refs/heads/") else ref
    return out


def parse_log(data) -> tuple:
    """`(commits, truncated)` out of `argv_log`'s answer: `sha8`, `author`,
    `at` (epoch seconds), `subject`, at most `MAX_COMMITS`."""
    rows = []
    for record in _text(data).split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 4:
            continue
        try:
            at = int(parts[2])
        except ValueError:
            at = 0
        rows.append({"sha8": parts[0].strip()[:8], "author": parts[1],
                     "at": at, "subject": "\x1f".join(parts[3:]).strip()})
    return rows[:MAX_COMMITS], len(rows) > MAX_COMMITS


def parse_numstat_z(data) -> tuple:
    """`(rows, truncated, total)` out of `argv_numstat_range`'s answer. A
    binary file is `binary: True` with no counts of its own (`added` and
    `removed` are 0); a path git quoted is dropped; the list is clamped to
    `work_record.MAX_FILES` with `total` honest."""
    rows = []
    for chunk in _text(data).split("\0"):
        chunk = chunk.lstrip("\n")
        if not chunk:
            continue
        parts = chunk.split("\t", 2)
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        if not path or path.startswith('"'):
            continue
        if added == "-" or removed == "-":
            rows.append({"path": path, "added": 0, "removed": 0,
                         "binary": True})
            continue
        try:
            rows.append({"path": path, "added": int(added),
                         "removed": int(removed), "binary": False})
        except ValueError:
            continue
    kept, truncated, total = work_record.clamp_files(rows)
    return kept, truncated, total


def parse_ahead_behind(text) -> tuple:
    """`(behind, ahead)` out of `argv_ahead_behind`'s answer, `(0, 0)` for
    anything else."""
    parts = _text(text).split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def parse_verdict(text) -> str:
    """`ship`, `stop` or `""`: read off the **first non-empty line** only —
    `VERDICT: SHIP` or `VERDICT: STOP — reason`, any case."""
    for line in _text(text).splitlines():
        if not line.strip():
            continue
        found = _VERDICT.match(line)
        return found.group(1).lower() if found else ""
    return ""


def verdict_word(verdict: str) -> str:
    return str(verdict or "").upper()


def review_current(review_tip, branch_tip) -> bool:
    """Whether a verdict was given for the branch as it stands."""
    return bool(review_tip) and str(review_tip) == str(branch_tip or "")


def conflict_note(trunk: str, files) -> str:
    return CONFLICT_NOTE.format(trunk, listed(files) or "some files")


def merged_note(trunk: str, tip: str, *, unchecked: bool = False,
                folder_kept: bool = False, branch_kept: bool = False) -> str:
    note = MERGED_NOTE.format(trunk, str(tip or "")[:8])
    if unchecked:
        note += MERGED_UNCHECKED_NOTE
    if folder_kept:
        note += FOLDER_KEPT_SUFFIX
    if folder_kept or branch_kept:
        note += BRANCH_KEPT_SUFFIX
    return note


def sha8(value) -> str:
    text = str(value or "")
    return text[:8] if text else ""
