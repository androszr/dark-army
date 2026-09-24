"""What Dark Army saw a run do — the pure half.

A dispatched session's `## Work done` report lives in the terminal and in one
pane of the panel, and both go away when the tab closes or the session is
stale-evicted. `close_note` is 400 characters and only exists where the agent
called `dark_army_close_card`. So a person on the phone, a day later, cannot tell
whether the outcome the card asked for was delivered.

This module holds everything about that record that is a *decision* rather
than an effect: the git argv, the parsers, every bound, the verdict rule and
the exact sentences both clients show verbatim. It runs no subprocess, opens
no file and imports nothing from the daemon.

**This is the only module in `host/` that names the `git` executable.** That
is a grep criterion, not a style note: the daemon has never run git before
(`workspace.py` states "No subprocess, no `git`" as a property of itself), and
keeping every argv here is what makes the whole capability auditable in one
read. Every call it builds is bounded by `GIT_TIMEOUT_SECONDS` and
`MAX_GIT_OUTPUT_BYTES` at the caller, carries `--no-optional-locks` so Dark Army
never takes the index lock out from under the person's own git, and runs under
`git_env()` so a credential helper can never block the daemon on a prompt.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from . import subprocess_env

#: The report text, matched to `session_stats.MAX_LAST_TEXT_CHARS` so the
#: `## Work done` write-up is never clipped twice — once on the way onto the
#: row and again on the way into the store — which would leave a record ending
#: mid-sentence for no reason a reader could see.
MAX_REPORT_CHARS = 4000
#: The one-line `bob-tldr` summary beside it, if the run left one.
MAX_SUMMARY_CHARS = 200
#: How many changed files the record lists. Past this the count is still
#: honest (`files_total`) — it is the *list* that is bounded, not the truth.
MAX_FILES = 200
#: A path longer than this is dropped from the list rather than truncated:
#: a truncated path resolves to something else, and a file row a person can
#: tap has to name a real file.
MAX_PATH_CHARS = 300
#: One file's diff text, on demand. 64 KB is a large file's whole rewrite and
#: far short of anything that would flood a phone.
MAX_DIFF_BYTES = 64_000
#: What Dark Army will read off a git pipe before giving up on the reading. A
#: working tree that produces more than this is one where the baseline no
#: longer means what the record would claim it means.
MAX_GIT_OUTPUT_BYTES = 256_000
#: Every git call is bounded by this. A repository with a pre-command hook, a
#: credential helper, a submodule walk or a 100k-file tree can be slow, and
#: nothing on the snapshot path may ever wait on one.
GIT_TIMEOUT_SECONDS = 8.0

#: How a run ended, as far as Dark Army could observe it. Never a claim about
#: whether the work is *good* — only about what the run said on its way out.
VERDICT_CLOSED = "closed"
VERDICT_MANUAL = "manual"
VERDICT_QUIET = "quiet"
VERDICTS = (VERDICT_CLOSED, VERDICT_MANUAL, VERDICT_QUIET)

#: The sentence every surface draws **verbatim**, `queue_reason`'s rule: the
#: wording is a decision about honesty and it is made once, here, rather than
#: three times in three languages. It says two things on purpose — that this
#: is Dark Army's observation and not the assistant's claim, and that at a parallel
#: limit above 1 two agents may edit one tree, so the file list is *what
#: changed in this project*, not *what this assistant changed*.
CAPTION = ("Dark Army watched this run and wrote this down; nothing here is the "
           "assistant's own claim. The files are what changed in this project "
           "since the work started, which can include another agent working "
           "in the same folder.")

#: One short sentence per verdict, drawn verbatim beside the caption.
VERDICT_WORDS = {
    VERDICT_CLOSED: "The assistant said it had finished this card.",
    VERDICT_MANUAL: "The assistant left a check for somebody to do by hand.",
    VERDICT_QUIET: "The run ended without the assistant saying anything.",
}

#: Why the file list is missing, in words. Stated rather than left to a zero:
#: "Dark Army could not read this" and "nothing changed" are different facts and a
#: person judging an outcome has to be able to tell them apart.
NO_BASELINE_REASON = ("Dark Army had no note of where this project stood when the "
                      "work started, so it cannot list the files.")
GIT_FAILED_REASON = "Dark Army could not read what changed in this project."
GIT_TIMEOUT_REASON = "Reading what changed in this project took too long."
TOO_MUCH_REASON = ("This run changed more than Dark Army is willing to read in one "
                   "go, so the file list was not taken.")
NOT_ENROLLED_REASON = ("This project is no longer one Dark Army watches, so it did "
                       "not read the folder.")
NO_ROOT_REASON = "Dark Army has no folder on record for this card."

#: Every key a stored record carries out to a client. Imported by the phone's
#: drift test, so a field added here without a Swift counterpart fails a test
#: rather than going quietly undrawn.
RECORD_KEYS = (
    "card_id", "run_at", "session_id", "root", "baseline", "verdict",
    "report", "summary", "files", "files_available", "files_reason",
    "files_changed", "files_total", "lines_added", "lines_removed",
    "recorded_at",
    # What the shunt skill's helper did during the run, folded off the
    # session's delegation ledger (`read_shunt_ledger`) when the run ends:
    # how many delegations, how many lines never entered the main model,
    # and what the helper cost where every delegation reported a figure —
    # `None` otherwise, never a made-up zero. Both clients draw the one
    # sentence `shunt_words` composes and count nothing themselves.
    "shunt_delegations", "shunt_lines_kept_out", "shunt_worker_cost_usd",
)

#: The ledger the shunt wrappers append to: one JSON object per line under
#: `paths.SHUNT_LEDGER_DIR / <session id>.jsonl`. Only the keys the fold
#: reads are named here; the wrappers write more (`at`, `model`, `files`,
#: `seconds`, `ok`) and a reader ignores what it does not know.
SHUNT_LEDGER_KEYS = ("delegation_id", "lines_kept_out", "worker_cost_usd")
#: How much of a ledger the fold will read. A session that delegated more
#: than this many bytes' worth of lines is one where the figure would be
#: wrong anyway; the read is bounded so a stray file cannot stall the loop.
MAX_SHUNT_LEDGER_BYTES = 4_000_000

#: Every key one file row carries. `added` / `removed` are **absent** on a
#: binary file rather than zero — git says `-`, and a zero there would read as
#: "this file changed by nothing".
FILE_KEYS = ("path", "added", "removed", "binary", "new")


# --- the argv ---------------------------------------------------------------

def _git(root: str) -> list:
    """The head of every call: no optional locks, no path quoting.

    `--no-optional-locks` keeps Dark Army out of `.git/index.lock`, which the
    person's own `git status` in that same tree is entitled to take.
    `core.quotePath=false` stops git escaping non-ASCII names into
    `"\\303\\251"`, which would reach a client as a path nothing can open.
    """
    return ["git", "--no-optional-locks", "-c", "core.quotePath=false",
            "-C", str(root)]


def argv_baseline(root: str) -> list:
    """Where this project stood the moment work started."""
    return _git(root) + ["rev-parse", "HEAD"]


def argv_numstat(root: str, baseline: str) -> list:
    """Tracked files changed since the baseline, with line counts."""
    return _git(root) + ["diff", "--numstat", str(baseline), "--"]


def argv_untracked(root: str) -> list:
    """Files that exist and git has never heard of. NUL-separated, because a
    newline is a legal character in a path and a line-separated list of paths
    is a list that lies about one."""
    return _git(root) + ["ls-files", "--others", "--exclude-standard", "-z"]


def argv_file_diff(root: str, baseline: str, rel: str) -> list:
    """One tracked file's changes since the baseline."""
    return _git(root) + ["diff", str(baseline), "--", str(rel)]


def argv_new_file_diff(root: str, rel: str) -> list:
    """One untracked file, against nothing. `--no-index` is what makes git
    diff a file it does not track; `os.devnull` is the empty left side."""
    return _git(root) + ["diff", "--no-index", "--", os.devnull, str(rel)]


def git_env(base=None) -> dict:
    """The environment every one of those runs under.

    `clean_env` is the frozen-bundle rule every other spawn here follows. The
    two git variables are this module's own: a repository with a credential
    helper must never be able to block the daemon on a prompt, and the
    optional-locks flag is belt and braces beside the argv's.
    """
    env = subprocess_env.clean_env(base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


# --- the parsers ------------------------------------------------------------

def _rename_target(raw: str) -> str:
    """The *new* path out of git's rename spelling.

    Two shapes: `old => new` for a whole path, and `dir/{a => b}/file` for a
    shared prefix. Anything else is returned untouched — a path is not a place
    to be clever.
    """
    if "=>" not in raw:
        return raw
    if "{" in raw and "}" in raw:
        head, rest = raw.split("{", 1)
        inner, tail = rest.split("}", 1)
        new = inner.split("=>")[-1].strip()
        joined = head + new + tail
        while "//" in joined:
            joined = joined.replace("//", "/")
        return joined
    return raw.split("=>")[-1].strip()


def parse_numstat(text: str) -> list:
    """`git diff --numstat` into file rows.

    A binary file is git's `-\t-\tpath`, and it comes back as `binary: True`
    with **no** `added` or `removed` key at all: zero would read as a file
    that changed by nothing, which is the one wrong answer here.

    A path git still quoted (a `"` or a newline in the name, which
    `core.quotePath=false` does not cover) is **dropped**: unescaping it here
    would be a second, worse copy of git's own quoting rules, and a row whose
    path is wrong is a row a person can tap to see somebody else's file.
    """
    rows = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, raw = parts[0], parts[1], "\t".join(parts[2:])
        path = _rename_target(raw).strip()
        if not path or path.startswith('"'):
            continue
        row = {"path": path, "binary": False, "new": False}
        if added == "-" or removed == "-":
            row["binary"] = True
        else:
            try:
                row["added"] = int(added)
                row["removed"] = int(removed)
            except ValueError:
                continue
        rows.append(row)
    return rows


def parse_untracked(data) -> list:
    """`git ls-files --others -z` into file rows, each marked `new`."""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = str(data or "")
    rows = []
    for chunk in text.split("\0"):
        path = chunk.strip()
        if not path or path.startswith('"'):
            continue
        rows.append({"path": path, "binary": False, "new": True})
    return rows


def clamp_files(rows: Iterable) -> tuple:
    """`(kept, truncated, total)`.

    `total` counts every row the parsers produced, including the ones dropped
    here — the count stays honest when the list cannot be. A path over
    `MAX_PATH_CHARS` is dropped rather than truncated for `parse_numstat`'s
    reason: a shortened path names a different file.
    """
    rows = list(rows or [])
    total = len(rows)
    kept = [r for r in rows
            if isinstance(r, dict) and 0 < len(str(r.get("path") or ""))
            <= MAX_PATH_CHARS]
    kept = kept[:MAX_FILES]
    return kept, len(kept) < total, total


def totals(rows: Iterable) -> tuple:
    """`(added, removed)` over the rows that carry counts. A binary file
    contributes nothing to either, which is the only honest arithmetic
    available — git did not count its lines and neither may Dark Army."""
    added = removed = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            added += int(row.get("added") or 0)
            removed += int(row.get("removed") or 0)
        except (TypeError, ValueError):
            continue
    return added, removed


def files_ok(rows) -> bool:
    """Whether a `files` payload is the parsers' own shape.

    `close_run` refuses on a False here rather than storing what it was given:
    a malformed list is a bug in the collector, and a record holding half of
    one reads to every surface as a successful reading of a project that
    barely changed.
    """
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not str(row.get("path") or ""):
            return False
        if set(row) - set(FILE_KEYS):
            return False
        for key in ("added", "removed"):
            if key in row and not isinstance(row[key], int):
                return False
        if not isinstance(row.get("binary", False), bool):
            return False
        if not isinstance(row.get("new", False), bool):
            return False
    return True


# --- the verdict ------------------------------------------------------------

def verdict_for(card, session_id: str) -> str:
    """How this run ended, from the card's own columns and nothing else.

    A hand-dragged Done card with no `closed_by` is `quiet` on purpose: the
    column already says a person moved it, and calling that a declaration
    would put the assistant's name on somebody else's decision.
    """
    card = card if isinstance(card, dict) else {}
    sid = str(session_id or "")
    closed_by = str(card.get("closed_by") or "")
    if str(card.get("column_name") or "") == "done" and sid and closed_by == sid:
        return VERDICT_CLOSED
    if str(card.get("manual_steps") or "").strip():
        return VERDICT_MANUAL
    return VERDICT_QUIET


def verdict_words(verdict: str) -> str:
    """The sentence for a verdict, or `""` for one this build does not know —
    an empty string a client draws nothing for, never an invented sentence."""
    return VERDICT_WORDS.get(str(verdict or ""), "")


# --- the delegation ledger ---------------------------------------------------

#: The characters a session id keeps in its ledger file name; everything
#: else becomes `_`. **The wrappers' rule, byte for byte** (`bulk_read.py`
#: / `code_write.py` `append_ledger`): the daemon has to read under the
#: same name they write under, or a card says 0 delegations for an id
#: with any other character in it.
_LEDGER_NAME_KEEP = "-_."


def ledger_name(session_id) -> Optional[str]:
    """The file name under `paths.SHUNT_LEDGER_DIR` the shunt wrappers
    append this session's delegations to — `<mapped id>.jsonl`, with every
    character outside `isalnum()` and `-_.` replaced by `_`, which is the
    wrappers' own map — or `None` when there is nothing safe to read: an
    empty id, or one that maps to `.` or `..`. `/` maps to `_`, so the
    name can never leave the ledger directory; a daemon that built the
    path from the raw id would have parsed an arbitrary same-user file for
    an id shaped like a path."""
    raw = str(session_id or "")
    mapped = "".join(c if (c.isalnum() or c in _LEDGER_NAME_KEEP) else "_"
                     for c in raw)
    if not mapped or mapped in (".", ".."):
        return None
    return mapped + ".jsonl"


def read_shunt_ledger(path) -> tuple:
    """`(delegations, lines_kept_out, worker_cost_usd)` off one session's
    ledger file, or `(0, 0, None)` where there is none.

    Tolerant on purpose: a line that is not a JSON object, or carries no
    `delegation_id`, is skipped; a duplicate id (a wrapper retried, a line
    appended twice) counts once; `lines_kept_out` is summed over the kept
    records as whole numbers, **never from a record whose `ok` is false**
    (a failed helper kept nothing out: the caller read the files another
    way, though the attempt still counts as a delegation); the cost is the
    USD sum **only when every
    kept delegation carried a measured number** — one missing figure makes
    the whole cost `None`, because a partial sum would read as the cost
    where it is a floor. Stdlib only; runs on the executor.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_SHUNT_LEDGER_BYTES)
    except (OSError, TypeError, ValueError):
        return 0, 0, None
    seen: set = set()
    lines_out = 0
    cost = 0.0
    cost_known = True
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        ident = record.get("delegation_id")
        if not isinstance(ident, str) or not ident.strip():
            continue
        if ident in seen:
            continue
        seen.add(ident)
        kept = 0 if record.get("ok") is False else record.get("lines_kept_out")
        if isinstance(kept, bool) or not isinstance(kept, int):
            try:
                kept = int(kept)
            except (TypeError, ValueError):
                kept = 0
        lines_out += max(kept, 0)
        usd = record.get("worker_cost_usd")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            cost_known = False
        else:
            cost += float(usd)
    if not seen:
        return 0, 0, None
    return len(seen), lines_out, (round(cost, 4) if cost_known else None)


def shunt_words(record) -> str:
    """The one sentence both clients draw for the helper's work, or `""`
    where the run delegated nothing (a client draws nothing for it).

    "3 delegations kept 2,140 lines out of the main model; helper cost
    $0.04" — or "…; helper cost unknown" where any delegation reported no
    figure, or "…; helper cost under $0.01" for a measured sub-cent figure
    (a "$0.00" would read as free). Composed once, here: neither client
    counts, sums or words it.
    """
    record = record if isinstance(record, dict) else {}
    try:
        count = int(record.get("shunt_delegations") or 0)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return ""
    try:
        kept = int(record.get("shunt_lines_kept_out") or 0)
    except (TypeError, ValueError):
        kept = 0
    noun = "delegation" if count == 1 else "delegations"
    lines = "line" if kept == 1 else "lines"
    usd = record.get("shunt_worker_cost_usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        cost = "helper cost unknown"
    elif 0 < float(usd) < 0.005:
        cost = "helper cost under $0.01"
    else:
        cost = f"helper cost ${float(usd):.2f}"
    return (f"{count} {noun} kept {kept:,} {lines} out of the main model; "
            f"{cost}")


def clamp_report(text) -> str:
    return str(text or "")[:MAX_REPORT_CHARS]


def clamp_summary(text) -> str:
    return str(text or "")[:MAX_SUMMARY_CHARS]


def clamp_diff(data) -> tuple:
    """`(text, truncated)` for one file's diff. Truncation is **stated**, and
    the cut is made on the decoded text so a multi-byte character is never
    split into a replacement glyph a reader would take for a change."""
    if isinstance(data, bytes):
        text = data[:MAX_DIFF_BYTES + 1].decode("utf-8", "replace")
    else:
        text = str(data or "")
    if len(text.encode("utf-8", "replace")) > MAX_DIFF_BYTES:
        return text[:MAX_DIFF_BYTES], True
    return text, False


def file_path_at(record, index) -> Optional[dict]:
    """The record's own file row at `index`, or None.

    **The caller supplies an index, never a path**, which is what makes the
    containment argument short: the only paths this can name are ones git
    itself produced into this record's own list.
    """
    if not isinstance(record, dict):
        return None
    files = record.get("files")
    if not isinstance(files, list):
        return None
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    if i < 0 or i >= len(files):
        return None
    row = files[i]
    return row if isinstance(row, dict) and str(row.get("path") or "") else None
