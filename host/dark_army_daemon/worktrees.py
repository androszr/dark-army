"""Card isolation with git worktrees — the pure half.

A started card works on its own branch, checked out in its own folder inside
the project (`<root>/.worktrees/card-<id8>/`), so two agents on one project
never edit the same tree and neither edits the person's main checkout. This
module holds every *decision* about that: the names, the git argv, the
environment the per-project setup script runs under and the sentences both
clients draw verbatim. The daemon (`daemon_board.py`) performs them: every
argv here runs through `BobDaemon._run_git`, bounded and on the executor.

It runs no subprocess, opens no file and imports nothing from the daemon
but `work_record`, whose `_git` head every argv here is built on — so
`--no-optional-locks` and `core.quotePath=false` ride every call, and
`work_record.git_env` is the environment. **The git argv live in two pure
modules, `work_record.py` and this one; every call runs through `_run_git`**
— a grep criterion, not a style note (`docs/card-worktrees.md`).

**No `--force`, anywhere.** `git worktree remove` without it refuses a tree
with modified tracked files or untracked files (ignored ones — `.venv`,
`node_modules` — do not count), and that refusal is the design: nothing
unsaved is ever destroyed, and the card says the folder was kept.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from typing import Optional

from . import work_record

#: The folder inside a project that holds every card's worktree. Git-ignored
#: through `EXCLUDE_LINE` in the repository's own `info/exclude`, and through
#: the starter `.gitignore` the agent pack offers.
WORKTREES_DIR = ".worktrees"
#: The line that keeps the worktrees out of `git status`. Anchored to the
#: repository root, so a nested folder of the same name is not swept up.
EXCLUDE_LINE = "/.worktrees/"
#: The person's optional per-project setup script, relative to the root. Run
#: once per new worktree (a virtual environment, installed packages, a copied
#: secrets file). Dark Army never creates one.
SETUP_SCRIPT = ".dark-army/worktree-setup.sh"
#: `git worktree add` checks a whole tree out; a large repository takes
#: seconds, never the eight `work_record.GIT_TIMEOUT_SECONDS` allows a read.
WORKTREE_ADD_TIMEOUT_SECONDS = 60.0
#: The fetch that makes "an up-to-date main" true. A failure (offline, no
#: credentials, a slow remote) is one log line and never a refusal.
FETCH_TIMEOUT_SECONDS = 20.0
#: The setup script's whole budget. The only bound on how long a card may sit
#: in the preparing state, since it has not started its bind window yet.
SETUP_TIMEOUT_SECONDS = 900.0
#: How much of the setup script's output the log beside the worktree keeps.
MAX_SETUP_LOG_BYTES = 256_000
#: The record of what was carried into a card's folder from the main
#: checkout's uncommitted pack update (`docs/card-worktrees.md`, *The pack
#: copies*), beside the setup log.
PACK_MANIFEST_SUFFIX = ".pack.json"
#: A carried pack file larger than this is skipped (and logged), never read.
MAX_PACK_COPY_BYTES = 1_000_000
#: The card-title part of a branch name, in characters.
MAX_SLUG_CHARS = 40
#: How many characters of the card's id name its branch and folder.
ID_CHARS = 8

#: The branch prefix and the folder prefix. A batch works on its head card's
#: branch in its head card's folder, so both are named after the head.
BRANCH_PREFIX = "card/"
FOLDER_PREFIX = "card-"

#: What a card says while Dark Army prepares its folder. Composed once here
#: and drawn verbatim on the card face and as the press's reply.
PREPARING_NOTE = ("Dark Army is preparing this card's own branch and folder — "
                  "the terminal opens when it is ready.")
#: A setup script that failed or ran out of time. `.format(code, log)`: the
#: exit code (or the words "timed out") and the log's path.
SETUP_FAILED_REFUSAL = ("the project's worktree setup script failed ({0}) — "
                        "see {1}, fix it and press Start again")
#: `git worktree add` refused (the folder is in the way, the branch is checked
#: out elsewhere, the repository has no commit yet).
ADD_FAILED_REFUSAL = ("Dark Army could not create this card's own branch and "
                      "folder — check the project's git state, or switch card "
                      "isolation off for this project and press Start again")
#: The setup script is there but the project has no trust decision for the
#: assistant being started (`trust_marks.root_trusted`). Dark Army runs a
#: project's script only for a folder its person has already trusted.
SETUP_UNTRUSTED_REFUSAL = ("this project has a worktree setup script, and Dark "
                           "Army runs it only in a folder you have already "
                           "trusted in this assistant — open the project in "
                           "it once and accept its trust question, or remove "
                           "the script, then press Start again")
#: The setup script is tracked by git, not owned by the person, or writable
#: by anybody else. `.format(script)`.
SETUP_UNSAFE_REFUSAL = ("Dark Army will not run {0}: the setup script must be "
                        "your own untracked file that nobody else can write — "
                        "untrack it, fix its owner or its permissions, then "
                        "press Start again")
#: The setup script sits on a disk that is not APFS, or Dark Army could not
#: tell what kind of disk it is. `.format(script)`. On Mac OS Extended (HFS+)
#: a name carrying an invisible Unicode character (U+200C ZERO WIDTH
#: NON-JOINER, U+FEFF, …) is stored as the name without it, so a file a
#: repository tracks under such a spelling lands in the person's own
#: `.dark-army` folder, and git's own listing cannot be matched against it.
SETUP_VOLUME_REFUSAL = ("Dark Army will not run {0}: the setup script runs "
                        "only on an APFS disk, and this project is on a disk "
                        "of another kind (or one Dark Army could not read) — "
                        "move the project to an APFS disk or remove the "
                        "script, then press Start again")
#: The index listing of the project's hidden files is larger than
#: `MAX_INDEX_DOTFILES_BYTES`, so Dark Army cannot check that the setup
#: script is not tracked. `.format(script)`.
SETUP_TOO_MANY_HIDDEN_REFUSAL = ("Dark Army will not run {0}: this project "
                                 "has too many hidden files for Dark Army to "
                                 "check that git does not track the setup "
                                 "script — remove the script, or switch card "
                                 "isolation off for this project, then press "
                                 "Start again")
#: The only kind of disk the setup script runs on (`statfs`'s
#: `f_fstypename`).
SETUP_VOLUME_TYPE = "apfs"
#: The cap on `argv_index_dotfiles`' output — its own, far above
#: `work_record.MAX_GIT_OUTPUT_BYTES`, because an ordinary project's hidden
#: folders (a Yarn cache, `.github`, `.vscode`) list thousands of entries.
MAX_INDEX_DOTFILES_BYTES = 16_000_000
#: `<root>/.worktrees` is a symbolic link: the worktrees would land wherever
#: it points. `.format(path)`.
WORKTREES_SYMLINK_REFUSAL = ("{0} is a link to another folder, so Dark Army "
                             "will not create this card's folder there — "
                             "replace it with a real folder, then press "
                             "Start again")
#: A finished card whose worktree still holds unsaved work. `.format(path)`.
KEPT_NOTE = ("This card's folder still has changes that were not committed, so "
             "Dark Army kept it: {0}")

_SLUG_RUN = re.compile(r"[a-z0-9]+")


# --- the names ---------------------------------------------------------------

def slug(title) -> str:
    """The card's title as a branch-name part: lowercase, runs of `[a-z0-9]`
    joined by `-`, clamped to `MAX_SLUG_CHARS` without a trailing `-`, and
    `work` when nothing is left. Never empty."""
    runs = _SLUG_RUN.findall(str(title or "").lower())
    text = "-".join(runs)[:MAX_SLUG_CHARS].strip("-")
    return text or "work"


def short_id(card_id) -> str:
    """The first `ID_CHARS` of the card's id, reduced to `[a-z0-9]` so a
    folder or branch name can never carry a separator or a dot-dot."""
    raw = "".join(_SLUG_RUN.findall(str(card_id or "").lower()))
    return raw[:ID_CHARS] or "card"


def branch_name(card: dict) -> str:
    """`card/<id8>-<slug>` — the branch a card (or a batch, by its head) works
    on."""
    card = card if isinstance(card, dict) else {}
    return f"{BRANCH_PREFIX}{short_id(card.get('id'))}-{slug(card.get('title'))}"


def worktree_dir(root: str, card_id) -> str:
    """`<root>/.worktrees/card-<id8>` — the folder the card's terminal opens
    in. Derived from the root, never a root of its own (`dispatch.py`'s
    fourth property)."""
    return os.path.join(str(root), WORKTREES_DIR,
                        f"{FOLDER_PREFIX}{short_id(card_id)}")


def checkout_root(path: str) -> str:
    """The main checkout that owns a card folder: `<root>` for
    `<root>/.worktrees/card-<id8>[/…]`, else `path` unchanged.

    A card's `root` is the checkout, never a card folder (`worktree_dir`'s
    rule). An agent working in a card folder files follow-up cards from its
    own cwd, and a card that names the card folder is refused once the
    folder is released ("no open window for that project"). The filing path
    and the board's open-time repair both map through here. Textual, on
    purpose: the folder may be gone already, so nothing is resolved."""
    text = str(path or "")
    marker = os.sep + WORKTREES_DIR + os.sep + FOLDER_PREFIX
    at = text.find(marker)
    if at <= 0 or len(text) == at + len(marker):
        return text
    return text[:at]


def setup_log_path(root: str, card_id) -> str:
    """`<root>/.worktrees/card-<id8>.setup.log` — beside the worktree, inside
    the excluded folder, so it is never an untracked file anywhere."""
    return os.path.join(str(root), WORKTREES_DIR,
                        f"{FOLDER_PREFIX}{short_id(card_id)}.setup.log")


def pack_manifest_path(root: str, card_id) -> str:
    """`<root>/.worktrees/card-<id8>.pack.json` — beside the setup log, inside
    the excluded folder, so it is never an untracked file anywhere."""
    return os.path.join(str(root), WORKTREES_DIR,
                        f"{FOLDER_PREFIX}{short_id(card_id)}{PACK_MANIFEST_SUFFIX}")


def setup_script_path(root: str) -> str:
    """Where the person's optional setup script would be."""
    return os.path.join(str(root), SETUP_SCRIPT)


def inside(root: str, path: str) -> bool:
    """Whether `path` is a folder under `<root>/.worktrees/`, compared by
    components after resolution. The one test the release applies before it
    names a folder to git: a stored path Dark Army did not derive is never
    removed."""
    if not root or not path:
        return False
    base = os.path.realpath(os.path.join(str(root), WORKTREES_DIR))
    target = os.path.realpath(str(path))
    return target != base and target.startswith(base.rstrip(os.sep) + os.sep)


# --- the argv ----------------------------------------------------------------

def argv_remote_url(root: str) -> list:
    """Whether the project has an `origin` to fetch from."""
    return work_record._git(root) + ["remote", "get-url", "origin"]


def argv_fetch(root: str) -> list:
    """Bring `origin` up to date so the branch starts from the latest main.
    `--quiet`, and no `--prune`: this reads, it never deletes a ref."""
    return work_record._git(root) + ["fetch", "--quiet", "origin"]


def argv_base_ref(root: str) -> list:
    """`origin/HEAD` as a short name (`origin/main`), or nothing."""
    return work_record._git(root) + [
        "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"]


def argv_branch_exists(root: str, branch: str) -> list:
    """Exit 0 when the local branch exists."""
    return work_record._git(root) + [
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]


def argv_worktree_add(root: str, path: str, branch: str,
                      base: str = "", *, existing: bool = False) -> list:
    """Check the card's branch out in its own folder. A new branch is created
    from `base`; an existing one (a card started before, whose folder was
    removed) is checked out as it is, commits and all."""
    if existing:
        return work_record._git(root) + ["worktree", "add", str(path), str(branch)]
    # `--no-track`: the card's branch is its own, never set to follow
    # `origin/main` (a later `git push` must not aim at the main line).
    return work_record._git(root) + [
        "worktree", "add", "--no-track", "-b", str(branch), str(path),
        str(base or "HEAD")]


def argv_is_ancestor(root: str, ancestor: str, descendant: str) -> list:
    """Exit 0 when `ancestor` is an ancestor of (or equal to) `descendant`.
    How the base is chosen: local `main` when `origin/HEAD` is behind or
    level with it (the person's unpushed work is on the base), otherwise
    `origin/HEAD`."""
    return work_record._git(root) + [
        "merge-base", "--is-ancestor", str(ancestor), str(descendant)]


def argv_setup_status(root: str, rel: str) -> list:
    """What git says about the setup script's path, NUL-separated, ignored
    and every untracked file listed. The gate allows only a **positive**
    untracked-or-ignored answer (`setup_status_allows`); a tracked file —
    under any case spelling on a case-insensitive disk, or inside a
    submodule — prints nothing, and nothing is refused."""
    return work_record._git(root) + [
        "status", "--porcelain", "-z", "--ignored=matching",
        "--untracked-files=all", "--", str(rel)]


def setup_status_allows(output: bytes, rel: str = SETUP_SCRIPT) -> bool:
    """Whether `argv_setup_status`'s output says, and says only, that the
    script is untracked or ignored. Exactly one entry, and it is one of:
    `?? <rel>`, `!! <rel>`, or `!! <its folder>/` — git's answer when the
    whole folder is ignored (an enrolled project ignores `.dark-army/`),
    which it gives only when nothing under that folder is tracked. Empty
    output, any other entry, or more than one is a refusal."""
    rel = str(rel)
    folder = rel.split("/", 1)[0] + "/"
    allowed = {f"?? {rel}\0", f"!! {rel}\0", f"!! {folder}\0"}
    try:
        text = bytes(output or b"").decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text in allowed


def argv_index_dotfiles(root: str) -> list:
    """Every index entry whose path starts with an ASCII `.`, NUL-separated
    and unquoted. The index half of the setup script's gate
    (`index_script_candidates`, `tracked_as_script`): a name can only share
    a folder with `.dark-army` on a case- and normalisation-insensitive disk
    if it starts with a literal `.` — the dot has no case or canonical
    variants — so this is every candidate, without listing a large
    repository whole."""
    return work_record._git(root) + ["ls-files", "-z", "--", ".*"]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def index_script_candidates(output) -> list:
    """The index entries that could be the setup script under another
    spelling: every entry whose first path component NFKC-normalises and
    casefolds to `.dark-army` (the Kelvin sign, a case variant with
    `core.ignorecase` off, a full-width letter), and any whose whole path
    folds to the script's own. Pure; the caller compares them on disk."""
    try:
        text = bytes(output or b"").decode("utf-8", "surrogateescape")
    except (TypeError, ValueError):
        return []
    folder = _fold(SETUP_SCRIPT.split("/", 1)[0])
    whole = _fold(SETUP_SCRIPT)
    out = []
    for entry in text.split("\0"):
        if not entry:
            continue
        if _fold(entry.split("/", 1)[0]) == folder or _fold(entry) == whole:
            out.append(entry)
    return out


def tracked_as_script(script_id, entry_ids) -> bool:
    """Whether any index entry is, on disk, the very file the setup script
    is: compared by `(st_dev, st_ino)`, never by spelling. Pure."""
    if not script_id:
        return False
    return any(tuple(e) == tuple(script_id) for e in entry_ids if e)


def argv_worktree_prune(root: str) -> list:
    """Forget worktrees whose folders are gone (removed by hand). Touches
    only git's own bookkeeping, never a folder that exists."""
    return work_record._git(root) + ["worktree", "prune"]


def argv_worktree_list(root: str) -> list:
    """Every worktree git knows, in the machine-readable form."""
    return work_record._git(root) + ["worktree", "list", "--porcelain"]


def argv_worktree_remove(root: str, path: str) -> list:
    """Remove a finished card's folder. **Never `--force`**: git refuses a
    tree with anything unsaved in it, and that refusal is the design."""
    return work_record._git(root) + ["worktree", "remove", str(path)]


#: The crew's own output folders. Every one is git-ignored, so `git worktree
#: remove` would delete a scout report, a plan or a check file written inside
#: a card's folder without a word — ignored files do not stop it.
CREW_OUTPUT = ("scout", "plans", "manual-check", "docs/research")


def argv_crew_output(path: str) -> list:
    """What git says about the crew's output folders inside one worktree,
    ignored entries included, NUL-separated (`holds_crew_output`)."""
    return work_record._git(path) + [
        "status", "--porcelain", "-z", "--ignored=matching", "--",
        *CREW_OUTPUT]


def holds_crew_output(output: bytes) -> bool:
    """Whether `argv_crew_output`'s answer names any file at all. Untracked
    and modified files already make git refuse the removal; the ignored
    `!!` entries are the ones this exists to catch."""
    return any(entry.strip() for entry in bytes(output or b"").split(b"\0"))


def argv_exclude_path(root: str) -> list:
    """Where this repository's `info/exclude` is — correct when `.git` is a
    file (the project is itself a linked worktree)."""
    return work_record._git(root) + ["rev-parse", "--git-path", "info/exclude"]


def argv_pack_status(root: str, pathspecs) -> list:
    """What differs from the index among the pack's pathspecs in the main
    checkout: changed, new and removed files, NUL-separated, one path per
    entry (`--no-renames`), every untracked file listed. A read."""
    return work_record._git(root) + [
        "--literal-pathspecs", "status", "--porcelain", "-z", "--no-renames",
        "--untracked-files=all", "--", *[str(p) for p in pathspecs]]


def argv_ls_files(root: str, paths) -> list:
    """Which of `paths` the index at `root` tracks, NUL-separated."""
    return work_record._git(root) + [
        "--literal-pathspecs", "ls-files", "-z", "--", *[str(p) for p in paths]]


def argv_ls_files_marked(root: str, pathspecs) -> list:
    """The index's per-file flag letters (`-v`) over the pack pathspecs,
    NUL-separated: `S <path>` is a skip-worktree entry (`parse_skipped`)."""
    return work_record._git(root) + [
        "--literal-pathspecs", "ls-files", "-v", "-z", "--",
        *[str(p) for p in pathspecs]]


def argv_cat_blob(root: str, rel: str) -> list:
    """`rel`'s committed bytes at HEAD, to put a carried file back when it
    could not be marked."""
    return work_record._git(root) + ["cat-file", "blob", f"HEAD:{rel}"]


def argv_ls_tree(root: str, rel: str) -> list:
    """`rel`'s tree entry at HEAD (`<mode> blob <sha>\\t<path>`, NUL-ended),
    for the mode a restored file gets back (`parse_tree_mode`)."""
    return work_record._git(root) + [
        "--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", str(rel)]


def parse_tree_mode(output) -> str:
    """The octal mode `argv_ls_tree` names (`100644`, `100755`, `120000`
    for a link), or `""` when it names nothing."""
    try:
        text = bytes(output or b"").decode("utf-8", "surrogateescape")
    except (TypeError, ValueError):
        return ""
    head = text.split("\0", 1)[0].split(" ", 1)[0]
    return head if head.isdigit() else ""


def argv_rm_cached(root: str, paths) -> list:
    """Drop an intent-to-add entry whose copy was taken away again."""
    return work_record._git(root) + [
        "--literal-pathspecs", "rm", "--cached", "-q", "--ignore-unmatch",
        "--", *[str(p) for p in paths]]


def argv_intent_to_add(root: str, paths) -> list:
    """Record an untracked-new carried file as intent-to-add, so that the
    skip-worktree mark after it keeps `git status` clean (verified on Apple
    Git 2.50.1: status clean, `git add -A` + commit leaves it out, and
    `git worktree remove` succeeds)."""
    return work_record._git(root) + [
        "--literal-pathspecs", "add", "-N", "--", *[str(p) for p in paths]]


def argv_skip_worktree(root: str, paths, skip: bool) -> list:
    """Mark (or un-mark) paths skip-worktree on the worktree's own index.
    Several paths ride one call after `--`."""
    flag = "--skip-worktree" if skip else "--no-skip-worktree"
    return work_record._git(root) + [
        "update-index", flag, "--", *[str(p) for p in paths]]


# --- the parsers and the text --------------------------------------------------

def parse_worktree_entries(text) -> list:
    """One dict per `worktree` stanza of `git worktree list --porcelain`:
    `path` (realpath'd), `prunable` (git would forget it), `locked` (git
    never prunes it) and `reason` (the text after `prunable `)."""
    entries: list = []
    current = None
    for line in str(text or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            if path:
                current = {"path": os.path.realpath(path), "prunable": False,
                           "locked": False, "reason": ""}
                entries.append(current)
            else:
                current = None
        elif current is None:
            continue
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
            current["reason"] = line[len("prunable"):].strip()
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    return entries


def parse_worktree_list(text) -> set:
    """The folder of every worktree in `git worktree list --porcelain`,
    realpath'd, so a stored path compares whatever the symlinks."""
    return {entry["path"] for entry in parse_worktree_entries(text)}


#: The one log sentence when a prune is refused: git would forget an entry
#: whose folder is still there (its own `.git` marker file was deleted).
PRUNE_BLOCKED_LINE = (
    "worktrees: not pruning {0}: git would forget {1}, a folder that is "
    "still there")


def prune_decision(entries, is_dir) -> tuple:
    """`(missing, blocked)` over `parse_worktree_entries`' answer. `missing`
    is every prunable entry whose folder is gone; `blocked` every prunable
    entry whose folder is still a directory (git prunes those too, when the
    folder's `.git` file was deleted). A locked entry is git's to keep and
    is in neither. Pure: `is_dir` is the caller's."""
    missing: list = []
    blocked: list = []
    for entry in entries or []:
        if not entry.get("prunable") or entry.get("locked"):
            continue
        (blocked if is_dir(entry["path"]) else missing).append(entry["path"])
    return missing, blocked


def base_from(symbolic: str) -> str:
    """The base ref out of `argv_base_ref`'s answer, or `""`."""
    text = str(symbolic or "").strip()
    return text if text and "\n" not in text else ""


def exclude_text(existing) -> Optional[str]:
    """`existing` with `EXCLUDE_LINE` appended once, or None when it is
    already there (nothing to write)."""
    text = str(existing or "")
    if any(line.strip() == EXCLUDE_LINE for line in text.splitlines()):
        return None
    if text and not text.endswith("\n"):
        text += "\n"
    return text + EXCLUDE_LINE + "\n"


def setup_folder_safe(st_mode: int, st_uid: int, uid: int) -> bool:
    """The `.dark-army` folder that holds the script: a real directory (not a
    link — `lstat`'s mode), owned by the person, writable by nobody else. A
    repository that commits `.dark-army` as a link to a tracked folder fails
    here."""
    return (stat.S_ISDIR(int(st_mode)) and int(st_uid) == int(uid)
            and not (int(st_mode) & 0o022))


def setup_volume_allows(fstype) -> bool:
    """Whether a disk of this kind may hold a setup script that runs: APFS
    alone. Anything else — Mac OS Extended, a network or FAT disk, or `""`
    when the kind could not be read — refuses (`SETUP_VOLUME_REFUSAL`)."""
    return str(fstype or "").strip().lower() == SETUP_VOLUME_TYPE


def setup_script_where(root_real: str) -> str:
    """Where the script must resolve to: the realpath'd root's own
    `.dark-army/worktree-setup.sh`, no link anywhere on the way."""
    return os.path.join(str(root_real), SETUP_SCRIPT)


def setup_script_safe(st_uid: int, st_mode: int, uid: int) -> bool:
    """The file half of the setup script's gate: owned by the person Dark
    Army runs as, and writable by nobody else (no group or other write
    bit). Tracked-by-git and trust are checked beside it."""
    return int(st_uid) == int(uid) and not (int(st_mode) & 0o022)


def setup_env(base, root: str, worktree: str, card_id: str,
              branch: str) -> dict:
    """The setup script's environment: `work_record.git_env` (the frozen
    bundle's variables gone, git unable to prompt) plus four names it may
    read."""
    env = work_record.git_env(base)
    env["DARK_ARMY_ROOT"] = str(root)
    env["DARK_ARMY_WORKTREE"] = str(worktree)
    env["DARK_ARMY_CARD_ID"] = str(card_id)
    env["DARK_ARMY_BRANCH"] = str(branch)
    return env


# --- the pack copies -----------------------------------------------------------

def parse_status_z(output) -> list:
    """`argv_pack_status`'s answer as `(XY, path)` pairs: each NUL-separated
    entry is `XY<space>path`; ignored (`!!`) and empty or malformed entries
    are dropped."""
    try:
        text = bytes(output or b"").decode("utf-8", "surrogateescape")
    except (TypeError, ValueError):
        return []
    out = []
    for entry in text.split("\0"):
        if len(entry) < 4 or entry[2] != " ":
            continue
        code, path = entry[:2], entry[3:]
        if code == "!!" or not path:
            continue
        out.append((code, path))
    return out


def pack_copy_plan(entries, is_pack_path, exists) -> list:
    """The pure decision for the pack copies: `{"path", "action"}` per path,
    `copy` when the main checkout holds it as a regular file, `delete` when
    it is absent there. `exists(path)` is injected so this opens nothing:
    True for a regular non-symlink file, False when absent, None when
    something else is there (a link, a folder) — dropped, never followed.
    A path `is_pack_path` refuses or that holds `..` is dropped; the git
    letters never decide anything, only the file does."""
    plan, seen = [], set()
    for _code, path in entries or []:
        path = str(path)
        if path in seen or ".." in path.split("/") or path.startswith("/"):
            continue
        if not is_pack_path(path):
            continue
        seen.add(path)
        found = exists(path)
        if found is True:
            plan.append({"path": path, "action": "copy"})
        elif found is False:
            plan.append({"path": path, "action": "delete"})
    return plan


def manifest_text(copies) -> str:
    """The manifest's JSON: `{"version": 1, "copies": [{"path", "kind":
    "tracked"|"new"|"deleted", "sha256"}]}`. Paths and digests only, never
    content."""
    rows = [{"path": str(c.get("path") or ""), "kind": str(c.get("kind") or ""),
             "sha256": str(c.get("sha256") or "")}
            for c in copies or [] if isinstance(c, dict) and c.get("path")]
    return json.dumps({"version": 1, "copies": rows}, indent=1)


def parse_skipped(output) -> list:
    """The paths `argv_ls_files_marked` shows as skip-worktree (`S `)."""
    try:
        text = bytes(output or b"").decode("utf-8", "surrogateescape")
    except (TypeError, ValueError):
        return []
    return [e[2:] for e in text.split("\0") if e.startswith("S ") and e[2:]]


def parse_manifest(text, is_pack_path=None) -> list:
    """The manifest's rows, or `[]` for anything malformed; unknown keys are
    ignored and a row without a path or with an unknown kind is dropped. A
    row whose path is absolute, holds `..` or (when `is_pack_path` is given)
    is not a pack path is dropped too — the file sits where an agent can
    write, so it is never trusted to name a path."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    rows = data.get("copies") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path, kind = row.get("path"), row.get("kind")
        if not isinstance(path, str) or not path \
                or kind not in ("tracked", "new", "deleted"):
            continue
        if path.startswith("/") or ".." in path.split("/"):
            continue
        if is_pack_path is not None and not is_pack_path(path):
            continue
        digest = row.get("sha256")
        out.append({"path": path, "kind": kind,
                    "sha256": digest if isinstance(digest, str) else ""})
    return out
