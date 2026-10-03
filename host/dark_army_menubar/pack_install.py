# host/dark_army_menubar/pack_install.py
"""The only module in ``host/`` that writes into a project root.

One allowlist, re-checked at the instant of the write: the project is still
enrolled, the destination is still inside it, and it is not Dark Army's own
checkout. Content-compared first, then a unique temp sibling + ``os.replace``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from dark_army_daemon import enrollment, paths, workspace
from dark_army_menubar import dev_build, pack_gitignore, pack_ledger, pack_render

logger = logging.getLogger("dark-army.menubar")

PACK_DESTINATIONS = (
    ".claude/leads/",
    ".claude/agents/",
    ".claude/skills/",
    ".claude/review.md",
    ".claude/settings.json",
    ".agents/skills/",
    ".codex/agents/",
    ".codex/config.toml",
    ".grok/agents/",
    ".grok/config.toml",
    ".github/workflows/",
    "scripts/",
    "docs/context.md",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "plans/.gitkeep",
    ".gitignore",
)

#: The project's ignore file. Not rendered: the pack *offers* lines into it
#: (`_offer_gitignore`), once per line per project, and never rewrites it.
GITIGNORE_KEY = ".gitignore"

#: Written only where the project does not already have the file. The
#: project's own copy is never compared and never replaced.
#:
#: The knowledge skill is *seed material*, not managed text: a project answers
#: its own questions, and a resync that reinstated the shipped catalogue would
#: throw those edits away. The cost is the other half of that bargain — a later
#: fix to the shipped skill never reaches a project that already has it, and
#: the escape hatch is deleting the file so the next resync re-seeds it. The
#: skill's own header says so.
#:
#: The `.agents` mirror keys are listed because `pack_render.mirror_skills`
#: synthesises them from the `.claude` copy: without them a resync would
#: rewrite the mirror from the shipped text while leaving the canonical copy
#: alone, which is the same clobber one directory over.
SEED_ONCE_KEYS = frozenset({
    ".claude/skills/knowledge/SKILL.md",
    ".claude/skills/knowledge/questions.md",
    ".agents/skills/knowledge/SKILL.md",
    ".agents/skills/knowledge/questions.md",
    ".agents/skills/knowledge/agents/openai.yaml",
})

SELF_MARKER = Path("host") / "build.sh"
SYNC_BUDGET_SECONDS = 20.0
_FILE_MODE = 0o644
_EXEC_MODE = 0o755
# Generated mirrors/shims a profile switch may prune. Canonical `.claude/`
# trees and `.github/workflows` are not in this list: a hand-added agent,
# a gitnexus-* skill, or a local workflow must survive a resync.
GENERATED_TREES = (
    ".agents/skills",
    ".codex/agents",
    ".grok/agents",
)
_RESYNC_LOCK = threading.Lock()
#: Set by a call that found a pass running; that pass then runs once more.
#: Read and written under `_RESYNC_GUARD`, with the release, so no request
#: lands in the gap between a pass's last check and its release.
_RESYNC_AGAIN = False
_RESYNC_GUARD = threading.Lock()
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.Lock] = {}


def is_pack_path(key: str) -> bool:
    """Whether ``key`` is a pack-owned path (the daemon's card worktrees
    carry these, never the constant itself)."""
    return _admissible(key)


def pack_pathspecs() -> tuple:
    """Each destination with its trailing ``/`` stripped — the pathspecs a
    ``git status`` over the pack takes."""
    return tuple(d.rstrip("/") for d in PACK_DESTINATIONS)


def _admissible(key: str) -> bool:
    """Relative, no ``..``, no leading ``/``, and inside the allowlist."""
    if not key or key.startswith("/") or key.startswith("\\"):
        return False
    parts = Path(key).parts
    if ".." in parts or (parts and parts[0] == "/"):
        return False
    posix = Path(key).as_posix()
    for dest in PACK_DESTINATIONS:
        if dest.endswith("/"):
            if posix == dest.rstrip("/") or posix.startswith(dest):
                return True
        elif posix == dest:
            return True
    return False


def _destination(root: str, key: str) -> Optional[Path]:
    """Real path of ``root/key``, or None if it escapes the project."""
    joined = os.path.realpath(os.path.join(root, key))
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        return None
    if not workspace._contains(folder, joined):
        return None
    return Path(joined)


def _is_bobs_own(root: str) -> bool:
    """True when ``root`` is Dark Army's own checkout.

    Two checks because ``find_repo_root()`` returns None for an installed
    release with no source stamp, and the marker is that function's own rule.
    """
    try:
        here = Path(enrollment.normalise(root))
    except (OSError, ValueError):
        here = Path(root)
    if (here / SELF_MARKER).is_file():
        return True
    repo = dev_build.find_repo_root()
    if repo is None:
        return False
    try:
        return enrollment.normalise(str(here)) == enrollment.normalise(str(repo))
    except (OSError, ValueError):
        return here.resolve() == repo.resolve()


def self_roots(candidates=None) -> list[str]:
    """Enrolled (or supplied) roots the write path would refuse as its own."""
    if candidates is None:
        try:
            candidates = enrollment.enrolled_roots()
        except Exception:
            candidates = []
    out = []
    for raw in candidates:
        try:
            folder = enrollment.normalise(str(raw))
        except (OSError, ValueError):
            continue
        if folder and _is_bobs_own(folder):
            out.append(folder)
    return sorted(set(out))


def _root_lock(root: str) -> threading.Lock:
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(root)
        if lock is None:
            lock = threading.Lock()
            _ROOT_LOCKS[root] = lock
        return lock


def _begin_write(root: str, *, wait: bool) -> bool:
    lock = _root_lock(root)
    if wait:
        lock.acquire()
        return True
    return lock.acquire(blocking=False)


def _end_write(root: str) -> None:
    try:
        _root_lock(root).release()
    except RuntimeError:
        pass


def user_profiles_dir() -> Path:
    """Where the person's own profiles live, read at call time so a test's
    patched ``paths`` is the one used. Dark Army never writes here."""
    return Path(paths.USER_PROFILES_PATH)


def _looks_like_pack(path: Path) -> bool:
    return path.is_dir() and (path / "template" / "CLAUDE.md").is_file()


def pack_root() -> Optional[Path]:
    """The vendored tree: bundle Resources, then package data, then checkout.

    None means leave every project alone — same posture as a missing close-out
    script.
    """
    bundle = dev_build.bundle_path()
    if bundle is not None:
        hit = bundle / "Contents" / "Resources" / "agent_pack"
        if _looks_like_pack(hit):
            return hit
    try:
        import importlib.resources
        packaged = importlib.resources.files("dark_army_menubar") / "agent_pack"
        as_path = Path(str(packaged))
        if _looks_like_pack(as_path):
            return as_path
    except Exception:
        pass
    repo = dev_build.find_repo_root()
    if repo is not None:
        hit = repo / "host" / "dark_army_menubar" / "agent_pack"
        if _looks_like_pack(hit):
            return hit
    local = pack_render.vendor_dir()
    if _looks_like_pack(local):
        return local
    return None


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(dest.parent), prefix=dest.name + ".", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.fchmod(fd, mode)
        os.close(fd)
        fd = -1
        os.replace(tmp, str(dest))
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _already_current(dest: Path, data: bytes, mode: int) -> bool:
    try:
        if not dest.is_file() or dest.read_bytes() != data:
            return False
        current = dest.stat().st_mode & 0o777
        if current == mode:
            return True
        os.chmod(dest, mode)
        return True
    except OSError:
        return False


def _write_one(root: str, key: str, data: bytes, mode: int) -> bool:
    """Write ``key`` if it changed. True when a replace happened."""
    if not _admissible(key):
        logger.warning("agent pack refused a path outside the allowlist: %s", key)
        return False
    dest = _destination(root, key)
    if dest is None:
        logger.warning("agent pack refused a path that escaped %s: %s", root, key)
        return False
    if _already_current(dest, data, mode):
        return False
    _atomic_write(dest, data, mode)
    return True


def _owned_for(root: str, field: str = "settings_allow_owned") -> list[str]:
    row = pack_ledger.entry(root)
    if not row:
        return []
    owned = row.get(field) or []
    return [str(item) for item in owned]


def _offered_for(root: str) -> list[str]:
    row = pack_ledger.entry(root)
    if not row:
        return []
    return [str(item) for item in (row.get("gitignore_offered") or [])]


def _digests_for(root: str) -> dict:
    return pack_ledger.digests_of(pack_ledger.entry(root))


def _offer_gitignore(folder: str, lines: list[str],
                     offered: list[str]) -> tuple[list[str], str]:
    """Append the starter ignore lines the project lacks and was never offered.

    Returns (the offered list to record, a note). Only where the folder is a
    git repository (``.git`` a folder or a worktree's file); anywhere else
    nothing is created and the list stays as it was, so a folder that later
    becomes a repository gets the block on its next resync. On any failure
    the list comes back **unchanged**, so the same lines are offered again
    next launch, and the note says what went wrong.
    """
    offered = [str(item) for item in offered]
    if not (Path(folder) / ".git").exists():
        logger.debug("agent pack: %s is not a git repository; no .gitignore", folder)
        return offered, ""
    # Never follow a link: `_destination` resolves one, and a committed
    # `.gitignore -> .git/hooks/pre-commit` would get the block appended to
    # an executable hook inside the project. Same rule as `_unlink_strays`.
    if (Path(folder) / GITIGNORE_KEY).is_symlink():
        logger.warning("agent pack: %s/.gitignore is a link; left alone", folder)
        return offered, "could not place .gitignore"
    dest = _destination(folder, GITIGNORE_KEY) if _admissible(GITIGNORE_KEY) else None
    if dest is None:
        return offered, "could not place .gitignore"
    existing = ""
    mode = _FILE_MODE
    try:
        if dest.exists() and not dest.is_file():
            return offered, "could not read .gitignore"
        if dest.is_file():
            existing = dest.read_text(encoding="utf-8")
            mode = dest.stat().st_mode & 0o777
    except (OSError, UnicodeDecodeError):
        return offered, "could not read .gitignore"
    new_text, appended, now = pack_gitignore.merge(existing, lines, set(offered))
    if appended:
        try:
            _atomic_write(dest, new_text.encode("utf-8"), mode)
        except OSError:
            logger.warning("agent pack could not write %s", dest)
            return offered, "could not write .gitignore"
    return sorted(set(offered) | set(now)), ""


def _with_note(result: str, note: str) -> str:
    return f"{result}; {note}" if note else result


def _is_gitnexus_skill(rel: str) -> bool:
    parts = Path(rel).parts
    return (
        len(parts) >= 3
        and parts[0] == ".agents"
        and parts[1] == "skills"
        and parts[2].startswith(pack_render.SKIP_SKILL_PREFIX)
    )


def _unlink_strays(root: str, mapping: dict[str, bytes]) -> None:
    """Drop generated mirrors the new profile no longer owns.

    Upstream's stray/orphan deletion for `.agents/skills`, `.codex/agents`
    and `.grok/agents` only. Never follows a symlink out of the project.
    """
    expected = set(mapping)
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        return
    base = Path(folder)
    for tree in GENERATED_TREES:
        raw = base / tree
        if raw.is_symlink():
            continue
        dest = _destination(root, tree)
        if dest is None or not dest.is_dir() or dest.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(str(dest), followlinks=False):
            dirnames[:] = [
                name for name in dirnames
                if not Path(dirpath, name).is_symlink()
                and not (
                    tree == ".agents/skills"
                    and name.startswith(pack_render.SKIP_SKILL_PREFIX)
                )
            ]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    rel = path.resolve().relative_to(base).as_posix()
                except (ValueError, OSError):
                    continue
                if rel in expected or not _admissible(rel):
                    continue
                if _is_gitnexus_skill(rel):
                    continue
                admitted = _destination(root, rel)
                if admitted is None:
                    continue
                try:
                    if admitted.resolve() != path.resolve():
                        continue
                except OSError:
                    continue
                try:
                    path.unlink()
                except OSError:
                    logger.warning(
                        "agent pack could not remove a leftover: %s", rel)


def _unlink_stale_leads(root: str, mapping: dict[str, bytes],
                        source: Optional[Path]) -> None:
    """Remove `.claude/leads/*.md` the new render no longer ships — only
    when the file's bytes are a brief the pack itself wrote
    (`pack_render.shipped_lead_digests`). A lead somebody wrote or edited
    has other bytes and stays. Never follows a symlink."""
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        return
    base = Path(folder)
    leads = base / ".claude" / "leads"
    if leads.is_symlink() or (base / ".claude").is_symlink() or not leads.is_dir():
        return
    digests = None
    for path in sorted(leads.glob("*.md")):
        rel = f"{pack_render.LEADS_DIR}{path.name}"
        if rel in mapping or path.is_symlink() or not path.is_file():
            continue
        if digests is None:
            digests = pack_render.shipped_lead_digests(source, user_profiles_dir())
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest() not in digests:
            continue
        admitted = _destination(root, rel)
        if admitted is None or admitted != path.resolve():
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("agent pack could not remove a leftover lead: %s", rel)


def _write_render(
    root: str,
    mapping: dict[str, bytes],
    *,
    owned: list[str],
    owned_deny: Optional[list[str]] = None,
    digests: Optional[dict] = None,
) -> tuple[int, list[str], list[str], str]:
    """Write ``mapping`` into ``root``.

    Returns (wrote, new_owned, new_owned_deny, note): the settings rows the
    pack owns after this write, allow and deny, for the ledger.

    ``digests`` is the ledger row's ``pack_digests`` (key -> the managed
    region the pack last wrote there), updated in place for every
    `pack_render.PROJECT_FILLED_KEYS` file the pack still owns.
    """
    executables = pack_render.executable_keys(mapping)
    wrote = 0
    note = ""
    new_owned = list(owned)
    new_owned_deny = list(owned_deny or [])
    if digests is None:
        digests = {}
    for key, data in mapping.items():
        payload = data
        if key in pack_render.PROJECT_FILLED_KEYS:
            dest = _destination(root, key)
            existing = b""
            if dest is not None and dest.is_file():
                try:
                    existing = dest.read_bytes()
                except OSError:
                    # Unreadable is not "absent": writing over a file we
                    # could not look at is the clobber this rule prevents.
                    continue
            fresh = pack_render.managed_digest(pack_render.splice(b"", data))
            current = pack_render.managed_digest(existing)
            if current and current != fresh and current != digests.get(key):
                # The project filled the template in. Its answers stand.
                continue
            # Absent, or hand-written with no markers yet (the region goes
            # above the project's text, which `splice` keeps), or still the
            # bytes the pack last wrote: the pack writes, and owns, it.
            digests[key] = fresh
            if _write_one(root, key, pack_render.splice(existing, data),
                          _FILE_MODE):
                wrote += 1
            continue
        if pack_render.project_adapted(key):
            dest = _destination(root, key)
            if dest is None:
                continue
            fresh = pack_render.file_digest(data)
            if dest.exists():
                try:
                    current = pack_render.file_digest(dest.read_bytes())
                except OSError:
                    # Unreadable is not "absent" (the context rule above).
                    continue
                if current != fresh and current != digests.get(key):
                    # The project adapted it, or it predates the ledger's
                    # record and differs: the project's copy stands.
                    logger.info("agent pack kept the project's own %s", key)
                    continue
            digests[key] = fresh
        if key in SEED_ONCE_KEYS:
            # First branch, above the splice and settings forks: seeding is a
            # decision about whether to write at all, and it must not be
            # reachable past a fork that has already merged something.
            #
            # `exists()` rather than `is_file()`: a directory sitting at that
            # path is also "already present" and must not be replaced.
            # `_destination` realpaths, so a *symlink* is judged by its target
            # — a link to a real file reads as present and is left alone. The
            # one gap that leaves is a **broken** link, which both predicates
            # call absent: there the seed proceeds and writes the link's
            # missing target, which `_destination` has already confirmed is
            # inside the project. Recorded rather than left accidental
            # (`test_knowledge_pack_seed.py`); a broken link in a skill folder
            # is already a project that needs a person.
            dest = _destination(root, key)
            if dest is None or dest.exists():
                continue
        if key in pack_render.SPLICED_KEYS:
            dest = _destination(root, key)
            existing = b""
            if dest is not None and dest.is_file():
                try:
                    existing = dest.read_bytes()
                except OSError:
                    existing = b""
            payload = pack_render.splice(existing, data)
        elif key == pack_render.SETTINGS_KEY:
            dest = _destination(root, key)
            if dest is not None and dest.is_file():
                try:
                    existing = dest.read_bytes()
                except OSError:
                    existing = b""
                try:
                    payload, new_owned, new_owned_deny = (
                        pack_render.merge_settings(
                            existing, data, owned, owned_deny))
                except pack_render.PackRenderError as exc:
                    note = str(exc)
                    continue
            else:
                new_owned = pack_render.settings_allow_rows(data)
                new_owned_deny = pack_render.settings_deny_rows(data)
        mode = _EXEC_MODE if key in executables else _FILE_MODE
        if _write_one(root, key, payload, mode):
            wrote += 1
    return wrote, new_owned, new_owned_deny, note



def detect_app(folder: str) -> str:
    """The Xcode project's name under ``ios/``, or "" when there is none.

    The ``{{APP}}`` placeholder names the .xcodeproj, the scheme and the
    entitlements files, and the renderer's fallback is the title-cased folder
    name — which is wrong the moment the app is not called after its repo
    (``finance-demo`` holding ``Ledgerly.xcodeproj`` gets its CI rewritten to
    ``FinanceDemo``, as a resync did to a sibling project twice, 2026-09-20/21).
    One project under ``ios/`` is the answer; zero or several means the
    renderer's fallback stands.
    """
    try:
        found = sorted(
            path.stem for path in (Path(folder) / "ios").glob("*.xcodeproj")
            if path.is_dir())
    except OSError:
        return ""
    return found[0] if len(found) == 1 else ""

def install_pack(
    root: str,
    profile: str,
    prefix: str,
    project: str,
    app: str = "",
    gitnexus_repo: str = "",
    models=None,
    efforts=None,
) -> tuple[bool, str, list[str]]:
    """Render and write one project. Returns (ok, detail, owned allow rows).

    ``models`` is the resolved per-role model table for this project
    (``agent_models.resolve``), handed straight to ``pack_render.render``;
    the renderer stays pure and this module reads no preference. ``efforts``
    is the same shape for the per-role effort table
    (``agent_models.resolve_efforts``); ``None`` renders no effort lines beyond
    the Codex shipped one.
    """
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        return False, "that folder is not a project Dark Army can write into", []
    if not folder or not Path(folder).is_dir():
        return False, "that folder is not a project Dark Army can write into", []
    if enrollment.root_enrolled(folder) != folder:
        return False, "that project is not enrolled", []
    if _is_bobs_own(folder):
        return False, (
            "Dark Army does not install the pack into its own project"
        ), []
    source = pack_root()
    if source is None:
        logger.warning("agent pack is not in this install; wrote nothing")
        return False, "the agent pack is not in this install", []
    app = app or detect_app(folder)
    try:
        mapping = pack_render.render(
            profile, prefix, project, app=app,
            gitnexus_repo=gitnexus_repo, source=source, models=models,
            user_dir=user_profiles_dir(), efforts=efforts)
    except pack_render.PackRenderError as exc:
        return False, str(exc), []
    try:
        pack_ledger.reserve(
            folder, profile=profile, prefix=prefix, project=project,
            app=app, gitnexus_repo=gitnexus_repo)
    except pack_ledger.PackLedgerError as exc:
        return False, str(exc), []
    _begin_write(folder, wait=True)
    try:
        if pack_ledger.entry(folder) is None:
            return False, "that project is no longer syncing", []
        owned = _owned_for(folder)
        owned_deny = _owned_for(folder, "settings_deny_owned")
        offered = _offered_for(folder)
        digests = _digests_for(folder)
        ignore_note = ""
        try:
            wrote, new_owned, new_owned_deny, note = _write_render(
                folder, mapping, owned=owned, owned_deny=owned_deny,
                digests=digests)
            if not note:
                _unlink_strays(folder, mapping)
                _unlink_stale_leads(folder, mapping, source)
                lines = pack_render.gitignore_lines(
                    profile, source, user_profiles_dir())
                offered, ignore_note = _offer_gitignore(folder, lines, offered)
        except Exception as exc:
            logger.exception("agent pack install into %s failed", folder)
            pack_ledger.update(folder, last_result="install failed")
            return False, str(exc), owned
        ok = not note
        detail = note or _with_note(
            "updated" if wrote else "already in step", ignore_note)
        last = note or _with_note("ok", ignore_note)
        pack_ledger.update(
            folder, settings_allow_owned=new_owned,
            settings_deny_owned=new_owned_deny,
            gitignore_offered=offered, pack_digests=digests,
            last_result=last,
            after_steps=pack_render.after_steps_for(
                profile, source, user_profiles_dir()))
        return ok, detail, new_owned
    finally:
        _end_write(folder)


OWN_MODEL_GLOBS = (".claude/agents/bc-*.md", ".codex/agents/bc-*.toml",
                   ".grok/agents/bc-*.md")


def pin_own_checkout(root: str, models, efforts=None) -> list[str]:
    """Pin the Agent models setting into Dark Army's own checkout, in place.

    The pack never renders into its own project (`_is_bobs_own`), so before
    22 Sep 2026 its ``bc-*`` briefs carried no ``model:`` line and every role
    — planner, implementer, verifier, reviewers, on Claude, Codex and Grok —
    ran on whatever the parent session ran. This rewrites only the model
    line of each existing ``bc-*`` brief and shim
    (`pack_render.pin_own_models`); it creates no file, follows no
    link and touches nothing else. ``efforts`` (the resolved effort table, or
    ``None``) pins the one ``effort`` line of each in the same pass. Returns
    the project-relative paths it rewrote. Never raises."""
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        return []
    if not folder or not _is_bobs_own(folder) or not isinstance(models, dict):
        return []
    base = Path(folder)
    mapping: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    try:
        found = [p for pattern in OWN_MODEL_GLOBS for p in base.glob(pattern)]
        for path in found:
            if path.is_symlink() or not path.is_file():
                continue
            key = path.relative_to(base).as_posix()
            mapping[key] = path.read_bytes()
            paths[key] = path
    except OSError:
        logger.exception("agent models: could not read %s's own briefs", folder)
        return []
    changed = pack_render.pin_own_models(mapping, models, efforts=efforts)
    if not changed:
        return []
    if not _begin_write(folder, wait=False):
        logger.info("agent models: %s is already being written; skipped", folder)
        return []
    wrote = []
    try:
        for key, data in sorted(changed.items()):
            path = paths[key]
            try:
                _atomic_write(path, data, path.stat().st_mode & 0o777)
                wrote.append(key)
            except OSError:
                logger.exception("agent models: could not pin %s", key)
    finally:
        _end_write(folder)
    logger.info("agent models pinned into Dark Army's own checkout: %d file(s)",
                len(wrote))
    return wrote


def resync_all(models_for=None, efforts_for=None) -> None:
    """Bring every pressed project back into step. Never raises.

    Single-flight: a second call while one is running is a no-op. Per-root
    locks skip a project an install is already writing. ``models_for`` is
    ``root -> {provider: {role: model}}`` — the caller's resolved Agent
    models table for that project — or ``None`` for a render carrying no
    model lines. ``efforts_for`` is the same for the effort table.
    """
    global _RESYNC_AGAIN
    with _RESYNC_GUARD:
        if not _RESYNC_LOCK.acquire(blocking=False):
            # A model press while a pass runs must not be lost: the running
            # pass goes round once more, reading the settings afresh.
            _RESYNC_AGAIN = True
            logger.info("agent pack resync already running; queued one more pass")
            return
        _RESYNC_AGAIN = False
    try:
        while True:
            _resync_all_locked(models_for, efforts_for)
            with _RESYNC_GUARD:
                if not _RESYNC_AGAIN:
                    _RESYNC_LOCK.release()
                    return
                _RESYNC_AGAIN = False
    except BaseException:
        with _RESYNC_GUARD:
            _RESYNC_AGAIN = False
            _RESYNC_LOCK.release()
        raise


def _resync_all_locked(models_for=None, efforts_for=None) -> None:
    # Dark Army's own checkout first: it is never rendered, so the one thing
    # the setting owes it is its model lines — and that needs no pack source.
    if models_for is not None:
        for own in self_roots():
            try:
                pin_own_checkout(
                    own, models_for(own),
                    efforts_for(own) if efforts_for is not None else None)
            except Exception:
                logger.exception("agent models: pinning %s failed", own)
    source = pack_root()
    if source is None:
        logger.info("agent pack is not in this install; skipped resync")
        return
    deadline = time.monotonic() + SYNC_BUDGET_SECONDS
    try:
        rows = pack_ledger.load().get("projects") or []
    except Exception:
        logger.exception("agent pack ledger could not be read")
        return
    for raw in rows:
        if time.monotonic() >= deadline:
            logger.info("agent pack resync hit its time budget; remainder next launch")
            return
        if not isinstance(raw, dict):
            continue
        root = str(raw.get("root") or "")
        if not root:
            continue
        try:
            models = None
            if models_for is not None:
                try:
                    models = models_for(root)
                except Exception:
                    logger.exception("agent pack: could not resolve the "
                                     "model table for %s", root)
            efforts = None
            if efforts_for is not None:
                try:
                    efforts = efforts_for(root)
                except Exception:
                    logger.exception("agent pack: could not resolve the "
                                     "effort table for %s", root)
            _resync_one(root, raw, source, models=models, efforts=efforts)
        except Exception:
            logger.exception("agent pack resync failed for %s", root)
            try:
                pack_ledger.update(root, last_result="resync failed")
            except Exception:
                logger.exception("could not record a failed pack resync")


def _resync_one(root: str, raw: dict, source: Path, models=None,
                efforts=None) -> None:
    live = pack_ledger.entry(root)
    if live is None:
        return
    try:
        folder = enrollment.normalise(root)
    except (OSError, ValueError):
        pack_ledger.update(root, last_result="not a folder")
        return
    if enrollment.root_enrolled(folder) != folder:
        logger.info("agent pack: %s is not enrolled; skipped", folder)
        pack_ledger.update(root, last_result="not enrolled")
        return
    if _is_bobs_own(folder):
        logger.info("agent pack: refusing Dark Army's own project")
        return
    profile = str((live.get("profile") or raw.get("profile") or ""))
    prefix = str((live.get("prefix") or raw.get("prefix") or ""))
    project = str(
        live.get("project") or raw.get("project")
        or Path(folder).name or "project")
    if not pack_render.valid_profile_id(profile) or not prefix:
        pack_ledger.update(root, last_result="incomplete record")
        return
    if not _begin_write(folder, wait=False):
        logger.info("agent pack: %s is already being written; skipped", folder)
        return
    try:
        if pack_ledger.entry(root) is None:
            return
        mapping = pack_render.render(
            profile, prefix, project,
            app=str(live.get("app") or raw.get("app") or detect_app(folder)),
            gitnexus_repo=str(
                live.get("gitnexus_repo") or raw.get("gitnexus_repo") or ""),
            source=source, models=models, user_dir=user_profiles_dir(),
            efforts=efforts)
        if pack_ledger.entry(root) is None:
            return
        owned = [str(item) for item in (live.get("settings_allow_owned") or [])]
        owned_deny = [
            str(item) for item in (live.get("settings_deny_owned") or [])]
        offered = [str(item) for item in (live.get("gitignore_offered") or [])]
        digests = pack_ledger.digests_of(live)
        ignore_note = ""
        _wrote, new_owned, new_owned_deny, note = _write_render(
            folder, mapping, owned=owned, owned_deny=owned_deny,
            digests=digests)
        if pack_ledger.entry(root) is None:
            return
        if not note:
            _unlink_strays(folder, mapping)
            _unlink_stale_leads(folder, mapping, source)
            lines = pack_render.gitignore_lines(
                profile, source, user_profiles_dir())
            offered, ignore_note = _offer_gitignore(folder, lines, offered)
        if pack_ledger.entry(root) is None:
            return
        pack_ledger.update(
            root,
            settings_allow_owned=new_owned,
            settings_deny_owned=new_owned_deny,
            gitignore_offered=offered,
            pack_digests=digests,
            last_result=note or _with_note("ok", ignore_note),
            after_steps=pack_render.after_steps_for(
                profile, source, user_profiles_dir()),
        )
    finally:
        _end_write(folder)
