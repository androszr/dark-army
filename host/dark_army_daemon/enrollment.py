# host/dark_army_daemon/enrollment.py
"""Which projects Dark Army is allowed to watch, and the key that says so.

Dark Army's hook socket (`paths.HOOK_SOCK_PATH`, private to this account; the
port `BOB_COMPANION_PORT` names is a bridge on its way out) authenticates
nothing: anything running as this user can open it and claim to be a session, file a card, or ask for a
terminal. This module is the enrolment boundary that answers that. A project is
enrolled by putting a private key file inside it (`<root>/.dark-army/key`,
0600, in a 0700 directory that ignores itself, gitignored) and recording its
**digest** — never the key — in `~/.dark-army/enrollment.json`. A project
enrolled before the move may hold only the old `<root>/.bob-companion/key`: it
is read second, by every reader, while the read window is open, and
`migrate_key_folders()` copies it under the new name on every launch. Every message arriving on the hook
socket carries the key its project holds; a message with no key, or with a key
no enrolled project holds, is turned away.

**The honest claim.** Everything here runs as one user, so a file readable by
the project is readable by any process running as that user. This is an
*enrolment and scoping* boundary in exactly the register `channel_server`'s
"the port is a lock, not a hiding place" uses. What it buys: an agent in an
untrusted repository cannot use Dark Army's board as a lateral channel into the
user's other projects, cannot file or close cards, and cannot make Dark Army open a
terminal. What it does not buy: it does not stop a local process that goes
looking for the key file, and nothing here should be read as though it does.

**The key identifies the project.** `resolve` matches on the digest alone; the
root a message *claims* is never consulted for admission, only for grouping. Two
roots holding the same key file means somebody copied it, which is a deliberate
human act and is not defended against.

**The key travels on every message, not once per session.** The notify script is
fire-and-forget and one-shot per event: there is no process to hold an admission
ticket and no reply leg to hand one down. Un-enrolment must bite on the next
thing a project says rather than at the end of a session that may run for hours.
And it is the house rule already — `delete_abandoned_agent` re-checks its
category at the moment of deletion, `dispatch_card` re-reads `board.db` at the
moment of dispatch. The cost is one small cached read per event on the client
and one dict lookup on the daemon.

**Fail closed.** An empty key returns "" before any comparison and an empty
ledger returns "", the same shape as `ChannelServer._authentic` and
`ApiServer._authorised`: an empty secret comparing equal to an absent field is
the exact hole.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from . import paths

logger = logging.getLogger("dark-army")

#: Where a project's key lives, relative to the project root: new name first.
#: The directory name is the state directory's own name (`~/.dark-army`), and
#: the legacy one is the state directory's old name — still present on a
#: machine moved to Dark Army, as a link to `~/.dark-army` — which is exactly
#: why the walk-up in every client **must** skip `Path.home()` under both
#: names, or a session anywhere under the home directory would find a `key`
#: there and enrol all of `$HOME`. That is the single most damaging bug
#: available here.
KEY_DIR_NAME = ".dark-army"
KEY_RELATIVE = Path(KEY_DIR_NAME) / "key"

#: The old key folder, **read-only** for the window: read behind the new name,
#: removed by an un-enrol, never written. Ending the window is a separate card.
LEGACY_KEY_DIR_NAME = ".bob-companion"
LEGACY_KEY_RELATIVE = Path(LEGACY_KEY_DIR_NAME) / "key"

#: Every place a key may be read from, in the order they are tried.
KEY_CANDIDATES = (KEY_RELATIVE, LEGACY_KEY_RELATIVE)

#: What is appended to a project's `.gitignore` on a (human) enrolment. Whole
#: directory, not the file: anything else Dark Army ever puts beside the key
#: is covered by the same line.
GITIGNORE_LINE = ".dark-army/"

#: The key folder's own `.gitignore`: it ignores everything in the folder,
#: itself included, so the folder never shows in `git status` even in a
#: project whose own `.gitignore` Dark Army was never allowed to edit.
KEY_DIR_IGNORE = "*\n"

#: A key is 43 characters of base64url. The cap is about a client that walks into
#: a directory holding something else entirely under that name.
MAX_KEY_BYTES = 512

#: How far a client may walk up looking for a key. A bound, not a policy: a cwd
#: forty levels deep is already pathological.
MAX_WALK_DEPTH = 40

LEDGER_VERSION = 1


# ── the secret ────────────────────────────────────────────────────────────────

def mint() -> str:
    """A new project key. `token_urlsafe(32)` — `api_server`'s own size."""
    return secrets.token_urlsafe(32)


def digest(key: str) -> str:
    """The ledger's record of a key. SHA-256 hex, and the *only* form of a key
    that is ever written to Dark Army's own disk or published anywhere."""
    return hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()


# ── the ledger ────────────────────────────────────────────────────────────────

#: `(st_mtime_ns, st_size)` of the ledger the cached parse belongs to, plus the
#: parse itself. The revision-keyed shape `_hidden_codex` uses: the gate runs on
#: the event loop for every hook event, and this makes that one `stat` in the
#: common case rather than a read and a parse.
_cache: tuple = (None, None)


def _revision(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load() -> dict:
    """The ledger, memoised on its own `(mtime_ns, size)`.

    Never raises: an unreadable or malformed ledger is an empty one, which
    fails closed (nothing is enrolled) rather than open.
    """
    global _cache
    path = paths.ENROLLMENT_PATH
    rev = _revision(path)
    cached_rev, cached = _cache
    if cached is not None and rev == cached_rev:
        return cached
    data: dict = {"version": LEDGER_VERSION, "projects": []}
    if rev is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            logger.warning("enrolment ledger unreadable; nothing is enrolled")
    projects = data.get("projects")
    if not isinstance(projects, list):
        data["projects"] = []
    else:
        data["projects"] = [p for p in projects if isinstance(p, dict)]
    _cache = (rev, data)
    return data


def invalidate() -> None:
    """Drop the memoised ledger. For tests, and for anything that just wrote."""
    global _cache
    _cache = (None, None)


def save(data: dict) -> bool:
    """Write the ledger atomically at 0600. Read-merge-write is the *caller's*
    job — everything here preserves whatever keys it was handed, top-level and
    per-project, so an older build never blanks a newer one's fields. Every
    good write also rewrites the agents' search-scope file
    (`search_scope.refresh`, which never raises)."""
    paths.ensure_state_dir()
    path = paths.ENROLLMENT_PATH
    try:
        # fsync-before-replace lives in the helper: the ledger is the authority
        # the enrolment gate reads, and a crash must not leave a truncated copy
        # wearing the real name.
        paths.atomic_write_json(path, data, mode=0o600, indent=2)
    except OSError:
        logger.warning("could not write the enrolment ledger", exc_info=True)
        return False
    invalidate()
    # Lazy: search_scope imports this module (the shape `normalise()` uses).
    from . import search_scope
    search_scope.refresh()
    return True


def projects() -> list:
    """Every enrolled project, oldest first. Digests included — this is the
    internal view; `snapshot()` is what may be published."""
    return list(load().get("projects") or [])


def enrolled_roots() -> set:
    """The normalised root of every enrolled project."""
    return {str(p.get("root") or "") for p in projects() if p.get("root")}


# ── the gate ──────────────────────────────────────────────────────────────────

def resolve(key: str) -> str:
    """The enrolled root whose key this is, or "".

    Fails closed twice over: an empty key returns before any comparison, and an
    empty ledger has nothing to match. Compared with `hmac.compare_digest`.
    """
    text = str(key or "").strip()
    if not text:
        return ""
    want = digest(text)
    for entry in projects():
        stored = str(entry.get("digest") or "")
        if not stored:
            continue
        if hmac.compare_digest(stored, want):
            return str(entry.get("root") or "")
    return ""


def _contains(folder: str, cwd: str) -> bool:
    """Is `cwd` inside `folder`? Component-aware — `/a/proj` does not contain
    `/a/project2`, which a bare `startswith` would happily claim. The rule is
    `workspace._contains`', reused rather than invented a second time."""
    return cwd == folder or cwd.startswith(folder + os.sep)


def normalise(root: str) -> str:
    """A directory as this module compares them. `dispatch.normalise_root`'s
    rule, and reused from it so `/tmp` and `/private/tmp` cannot disagree."""
    from . import dispatch
    return dispatch.normalise_root(root)


def root_enrolled(cwd: str) -> str:
    """The longest enrolled root containing `cwd`, or "".

    This is the filter for the three sources that never knock — Codex, Grok and
    the background-agent roster all come off files Dark Army reads, so there is no
    message to refuse and their rows are dropped instead.
    """
    if not cwd:
        return ""
    try:
        here = normalise(cwd)
    except (OSError, ValueError):
        return ""
    if not here:
        return ""
    best = ""
    for root in enrolled_roots():
        if _contains(root, here) and len(root) > len(best):
            best = root
    return best


def enrolled_label(cwd: str) -> str:
    """What to *call* the project `cwd` sits in: the basename of the longest
    enrolled root containing it, or "".

    **Naming, not admission, and the two must not share a symbol.** This is
    deliberately implemented over `enrolled_roots()` + `_contains` rather than
    by calling `root_enrolled` — `tests/conftest.py`'s autouse
    `_open_the_enrolment_door` patches that *name* to an identity stub so a
    test can be about what happens past the door, and if naming read it, every
    label in the suite would silently become `basename(cwd)`. Reading the
    ledger directly means naming sees the (empty) tmp ledger the fixture
    already points at, and is inert for every existing test.

    The ledger's stored `label` field is deliberately **ignored**: the folder's
    own basename is the rule, with no exceptions, and honouring a stored label
    would turn a hand-edited ledger into a renaming affordance nobody designed.
    """
    if not cwd:
        return ""
    try:
        here = normalise(cwd)
    except (OSError, ValueError):
        return ""
    if not here:
        return ""
    best = ""
    for root in enrolled_roots():
        if _contains(root, here) and len(root) > len(best):
            best = root
    return _label(best) if best else ""


# ── enrol / un-enrol ──────────────────────────────────────────────────────────

def _label(root: str) -> str:
    return os.path.basename(root.rstrip(os.sep)) or root


def _append_gitignore(root: Path) -> str:
    """Add `GITIGNORE_LINE` to this project's `.gitignore`, once.

    Only where the project is a git repository: a `.gitignore` created in a
    folder that has no `.git` is litter in somebody's directory. Idempotent —
    a line that already equals ours after stripping is left alone, so enrolling
    twice does not add it twice.
    """
    if not (root / ".git").exists():
        return "no .gitignore written (this folder is not a git repository)"
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return "could not read .gitignore"
    for line in existing.splitlines():
        if line.strip() == GITIGNORE_LINE:
            return ""
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{GITIGNORE_LINE}\n")
    except OSError:
        return "could not write .gitignore"
    return ""


def _key_link(path: Path) -> bool:
    """Is the key file at `path`, or the key folder holding it, a symlink?

    Both sit in a project's tree, and a repository can ship either as a link
    to a private file elsewhere; reading through it would adopt that file's
    first bytes as a key, and a copy would publish them in the project.
    """
    try:
        return path.parent.is_symlink() or path.is_symlink()
    except OSError:
        return True


def _read_file_key(path: Path) -> str:
    """The stripped key in `path`, or "" on anything unreadable.

    **Never through a link**: a key folder that is a symlink is refused and
    the file is opened `O_NOFOLLOW`, the rule `_write_key_dir` applies to
    writes.
    """
    if _key_link(path):
        return ""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            return fh.read(MAX_KEY_BYTES).strip()
    except (OSError, ValueError):
        return ""


def _read_key_at(root: Path) -> tuple:
    """`(key, relative path it came from)` for `root`, new name first, or
    `("", None)` when neither folder holds a non-empty key."""
    for relative in KEY_CANDIDATES:
        key = _read_file_key(root / relative)
        if key:
            return key, relative
    return "", None


def _write_key_dir(root: Path) -> None:
    """Create `<root>/.dark-army` at 0700 and its self-ignoring `.gitignore`.

    The ignore file is written only when absent (temp sibling + `os.replace`,
    0600), so a person's edit to it survives. Raises `OSError`.

    **A key folder that is a symlink is refused.** The folder sits in a
    project's tree, and a repository can ship `.dark-army` as a link to
    anywhere this user can write; following it would `chmod` and write into
    that place. The same reason the key file is opened `O_NOFOLLOW`.
    """
    folder = root / KEY_DIR_NAME
    if folder.is_symlink():
        raise OSError(f"{folder} is a symlink; refusing to follow it")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(folder), 0o700)
    ignore = folder / ".gitignore"
    if ignore.exists():
        return
    tmp = folder / f".gitignore.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, KEY_DIR_IGNORE.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(str(tmp), str(ignore))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_ignore(root: Path) -> str:
    """The text of `<root>/.dark-army/.gitignore`, never through a link, or
    "" when it cannot be read."""
    try:
        fd = os.open(str(root / KEY_DIR_NAME / ".gitignore"),
                     os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            return fh.read(4096)
    except (OSError, ValueError):
        return ""


def _write_key_file(path: Path, key: str) -> None:
    """Create the key file at 0600, never over an existing one.

    Raises `FileExistsError` when something is already there (a concurrent
    enrol) and `OSError` on anything else.
    """
    fd = os.open(str(path),
                 os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, key.encode("utf-8"))
    finally:
        os.close(fd)


def enroll(root: str) -> tuple:
    """Put a key inside `root` and record it. `(ok, message)`.

    Refuses `/` and `$HOME` exactly: both are "every project at once", and the
    home directory is also where the state folder's old `.bob-companion` name lives. Re-enrolling
    an already-enrolled root is a no-op that **keeps the existing key** — a new
    one would silently orphan every session already running in that project.
    A root holding only the old `.bob-companion/key` gets that same key copied
    to `.dark-army/key`, never a fresh one. Writes only the new name.
    """
    try:
        here = Path(normalise(root))
    except (OSError, ValueError):
        return False, "that is not a folder Dark Army can read"
    if not str(here) or not here.is_dir():
        return False, "that is not a folder"
    if str(here) == os.sep:
        return False, "the whole filesystem is not a project"
    if here == Path(os.path.realpath(str(Path.home()))):
        return False, "your home folder is not a project — enrol the projects inside it"

    data = load()
    entries = list(data.get("projects") or [])
    for entry in entries:
        if str(entry.get("root") or "") == str(here):
            return True, f"{_label(str(here))} is already enrolled"

    key_path = here / KEY_RELATIVE
    # A key file left behind by a failed un-enrol, by a copy from another
    # machine, or under the old folder name from before the move. The *ledger*
    # is the authority, so adopting the file re-admits nothing that was not
    # just enrolled by this call — and adopting rather than minting keeps
    # every session already running in that project on the same digest.
    key, found_at = _read_key_at(here)
    try:
        _write_key_dir(here)
    except OSError:
        return False, "could not create the key folder inside that project"

    if found_at != KEY_RELATIVE:
        if not key:
            key = mint()
        try:
            _write_key_file(key_path, key)
        except FileExistsError:
            key = _read_file_key(key_path)
        except OSError:
            return False, "could not write the key file"
    if not key:
        return False, "could not write the key file"

    note = _append_gitignore(here)
    entries.append({
        "root": str(here),
        "digest": digest(key),
        "label": _label(str(here)),
        "enrolled_at": time.time(),
    })
    data["projects"] = entries
    if not save(data):
        return False, "could not record the enrolment"
    message = f"{_label(str(here))} is enrolled"
    return True, message if not note else f"{message} — {note}"


def unenroll(root: str) -> tuple:
    """Forget `root` and best-effort delete its key file. `(ok, message)`.

    **The ledger is the authority**, so a key file a failed delete leaves behind
    cannot re-admit anything: `resolve` matches digests in the ledger and the
    entry is gone.
    """
    try:
        here = normalise(root)
    except (OSError, ValueError):
        here = str(root or "")
    data = load()
    entries = list(data.get("projects") or [])
    kept = [e for e in entries if str(e.get("root") or "") != here]
    if len(kept) == len(entries):
        return False, "that project is not enrolled"
    data["projects"] = kept
    if not save(data):
        return False, "could not record the un-enrolment"
    for relative in KEY_CANDIDATES:
        path = Path(here) / relative
        try:
            folder_is_link = path.parent.is_symlink()
        except OSError:
            folder_is_link = True
        if folder_is_link:
            # Unlinking through it would delete a file outside the project.
            logger.info("left %s alone in %s: its key folder is a symlink",
                        relative, here)
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.info("left the key file %s behind in %s; the ledger is the "
                        "authority", relative, here)
    return True, f"{_label(here)} is no longer enrolled"


def migrate_key_folders() -> list:
    """Copy every enrolled project's old-name key to `.dark-army/key`.

    Run once per launch, on a worker thread (`app._migrate_enrolment_keys`).
    Returns one plain line per thing worth logging; never raises.

    - **Never overwrites.** A `.dark-army/key` already present is left alone;
      if it differs from the old-name key both stay and a line says so (two
      machines on one synced checkout may have minted different keys, and the
      ledger's single digest cannot say which one another machine holds).
      Every reader then sends `.dark-army/key`, so the project's sessions are
      refused unless that key is the enrolled one — the line says so.
    - **Never reads or writes through a link.** An old key file or key folder
      that is a symlink is refused with a line, and so is a `.dark-army`
      folder that is one.
    - **Copies only into a folder that ignores itself.** A `.dark-army/.gitignore`
      the repository shipped that is not exactly `KEY_DIR_IGNORE` would let
      the copied key show in `git status`; the copy is skipped with a line,
      since nobody pressed anything that could decide to edit it.
    - **Never writes the ledger** (the copied key's digest is the digest
      already recorded), **never opens the project's `.gitignore`** (the
      folder ignores itself) and **never unlinks the old file**.
    - **Never writes outside an enrolled root.** The root comes from the
      ledger and is re-checked against `/` and the home folder, the two
      refusals `enroll` makes, because the ledger could be hand-edited.
    - Idempotent: once every key is copied, a second call writes nothing and
      returns only the "differs" lines, if any (plus the "no key file" line
      for a root that has none under either name).
    """
    lines: list = []
    try:
        home = os.path.realpath(str(Path.home()))
    except (OSError, RuntimeError):
        home = ""
    for entry in projects():
        raw = str(entry.get("root") or "")
        if not raw or raw == os.sep:
            continue
        try:
            real = os.path.realpath(raw)
        except (OSError, ValueError):
            continue
        if real == os.sep or (home and real == home):
            continue
        root = Path(raw)
        label = _label(raw)
        new_path = root / KEY_RELATIVE
        old_path = root / LEGACY_KEY_RELATIVE
        try:
            if new_path.exists():
                if old_path.exists():
                    new_key = _read_file_key(new_path)
                    old_key = _read_file_key(old_path)
                    if old_key and new_key != old_key:
                        line = (f"{label}: {KEY_RELATIVE} differs from "
                                f"{LEGACY_KEY_RELATIVE}; left both. The "
                                f"project now sends {KEY_RELATIVE}, and its "
                                "sessions may be refused unless that key is "
                                "the enrolled one")
                        logger.warning(line)
                        lines.append(line)
                continue
            if _key_link(old_path):
                lines.append(f"{label}: the old key is a symlink; not copied")
                continue
            if not old_path.exists():
                lines.append(f"{label}: no key file on disk; left as is")
                continue
            key = _read_file_key(old_path)
            if not key:
                lines.append(f"{label}: the old key file is empty; left as is")
                continue
            _write_key_dir(root)
            if _read_ignore(root) != KEY_DIR_IGNORE:
                lines.append(f"{label}: {KEY_DIR_NAME}/.gitignore does not "
                             "ignore the folder; key not copied")
                continue
            try:
                _write_key_file(new_path, key)
            except FileExistsError:
                # A concurrent enrol got there first; the next pass compares.
                continue
            lines.append(f"{label}: copied the key to {KEY_RELATIVE}")
        except OSError as exc:
            lines.append(f"{label}: could not copy the key ({exc.__class__.__name__})")
    return lines


def enroll_self() -> tuple:
    """Enrol Dark Army's own checkout, so an upgrade does not silence the machine.

    A no-op with no source tree, and a no-op once enrolled. Nothing else enrols
    itself: every other project waits for the user.
    """
    try:
        from dark_army_menubar import dev_build
        root = dev_build.find_repo_root()
    except Exception:
        logger.debug("could not locate Dark Army's own repo root", exc_info=True)
        return False, "no source tree to enrol"
    if root is None:
        return False, "no source tree to enrol"
    return enroll(str(root))


# The memo for `self_root()`: `None` until the first call, then the answer for
# the life of the process (`""` included). A test resets it to `None`.
_SELF_ROOT: str | None = None


def self_root() -> str:
    """Dark Army's own checkout, normalised as this module compares roots, or "".

    The root `enroll_self()` enrols, found the same way, so the first-run
    checklist can tell it apart from a folder the person enrolled (the
    checklist never follows it or offers it). **Memoised per process**: the
    ancestor walk and the `repo-root` stamp read happen once, never per
    snapshot cycle; a stamp that appears later is a restart away. `""` when
    there is no source tree or the lookup fails.
    """
    global _SELF_ROOT
    if _SELF_ROOT is not None:
        return _SELF_ROOT
    found = ""
    try:
        from dark_army_menubar import dev_build
        root = dev_build.find_repo_root()
        if root is not None:
            found = normalise(str(root))
    except Exception:
        logger.debug("could not locate Dark Army's own repo root", exc_info=True)
        found = ""
    _SELF_ROOT = found or ""
    return _SELF_ROOT


# ── what may be published ─────────────────────────────────────────────────────

def snapshot() -> dict:
    """The enrolled projects, as a surface may see them.

    **Digests and keys never appear here**, and a test asserts it. `available`
    is stated rather than inferred from an empty list: an older daemon sends no
    section at all, and an empty list decoding as "nothing is enrolled" would
    draw the enrolment prompt over a working fleet.
    """
    rows = []
    for entry in projects():
        root = str(entry.get("root") or "")
        if not root:
            continue
        rows.append({"root": root,
                     "label": str(entry.get("label") or _label(root))})
    rows.sort(key=lambda r: r["label"].lower())
    return {"available": True, "enrolled": rows}
