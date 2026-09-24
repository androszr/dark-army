"""Freshness gates for ``host/build.sh``: the decisions, not the build.

``build.sh`` used to be forgiving — a failed ``swift build`` or ``vsce package``
was a WARNING, after which it bundled whatever old binary sat in
``panel/.build`` and the highest-versioned ``.vsix`` lying in the checkout. A
release could therefore ship yesterday's panel or a stale extension while
looking perfectly healthy. This module answers the two questions the script
now refuses on:

* :func:`panel_freshness` — is ``panel/.build/release/BobPanel`` at least as
  new as every source that is linked into it, and does its resource bundle
  carry every file under ``Sources/BobPanel/Resources`` unchanged?
* :func:`vsix_match` — does the one package the build would ship carry the
  version ``vscode-extension/package.json`` declares, both in its filename and
  inside the archive, and (for a strict build) was it written by this run?
* :func:`lock_match` — does ``vscode-extension/package-lock.json`` still
  describe the manifest beside it, so ``npm ci`` installs the recorded set
  rather than dying with npm's own ``EUSAGE`` wall?
* :func:`home_path_verdict` — does the finished bundle a release zips up
  carry the builder's home folder anywhere, inside zipped files included?

It also writes the provenance note every bundle carries
(:func:`build_manifest` / :func:`write_manifest`): what the app was actually
built from, dropped into ``Contents/Resources`` beside the repo-root stamp and
before the bundle is signed.

It is called as a subprocess — ``python -m dark_army_menubar.build_check
panel|vsix …`` — by ``build.sh`` and by its tests. **Stdlib only, and never
imported by the menu-bar runtime**: it lives inside ``dark_army_menubar``
solely so ``.venv/bin/python -m …`` resolves it and py2app's ``packages`` list
freezes it with no ``setup.py`` change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

PANEL_SOURCE_DIR = Path("panel") / "Sources" / "BobPanel"
PANEL_RESOURCE_DIR = PANEL_SOURCE_DIR / "Resources"
PANEL_PACKAGE = Path("panel") / "Package.swift"
PANEL_INFO_PLIST = PANEL_SOURCE_DIR / "Info.plist"
PANEL_BINARY = Path("panel") / ".build" / "release" / "BobPanel"
PANEL_BUNDLE = Path("panel") / ".build" / "release" / "BobPanel_BobPanel.bundle"

VSIX_PREFIX = "dark-army-ide-"
# `vscode_extension._VSIX_RE`, restated: that module imports the
# `vscode_reveal` chain, which a fake-tree test on a bare PYTHONPATH must not
# need.
_VSIX_RE = re.compile(r"dark-army-ide-(\d+\.\d+\.\d+)\.vsix$")

STALE_ADVICE = "rm -rf panel/.build and rerun if swift build keeps leaving an old binary"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    path: str = ""


# --- panel -----------------------------------------------------------------


def _root(repo_root) -> Path:
    """Every path judgement here is made on a normalised root. `build.sh` calls
    the helper with ``$SCRIPT_DIR/..``, so an un-normalised root carries a
    literal ``..`` component — which a per-component dotfile test reads as a
    hidden file and skips, silently emptying the resource walk in the one
    caller that matters."""
    return Path(repo_root).resolve()


def panel_linked_sources(repo_root: Path) -> List[Path]:
    """Every file whose change must relink the panel binary: all ``*.swift``
    under the target (recursively, so a future subfolder cannot escape the
    check), ``Package.swift`` and the ``Info.plist`` welded into ``__TEXT``."""
    root = _root(repo_root)
    sources = sorted(p for p in (root / PANEL_SOURCE_DIR).rglob("*.swift") if p.is_file())
    for extra in (root / PANEL_PACKAGE, root / PANEL_INFO_PLIST):
        if extra.is_file():
            sources.append(extra)
    return sources


_RESOURCE_DECL_RE = re.compile(r'\.(?:copy|process)\(\s*"Resources/([^"]+)"\s*\)')


def panel_resource_dirs(repo_root: Path) -> List[Path]:
    """The resource directories ``Package.swift`` actually declares, resolved
    under the target. Only these reach the built bundle, so only these may be
    demanded of it: a stray file directly in ``Resources/`` — a ``.DS_Store``
    Finder writes while somebody browses the cast PNGs — is copied nowhere and
    would otherwise refuse every build for ever, with `rm -rf panel/.build` no
    cure."""
    root = _root(repo_root)
    try:
        manifest = (root / PANEL_PACKAGE).read_text(encoding="utf-8")
    except OSError:
        return []
    out: List[Path] = []
    for name in _RESOURCE_DECL_RE.findall(manifest):
        target = root / PANEL_RESOURCE_DIR / name
        if target.is_dir() and target not in out:
            out.append(target)
    return out


def manifest_declares_resources(repo_root: Path) -> bool:
    """Whether ``Package.swift`` has a ``resources:`` key at all. Parsing it is
    a regex over somebody else's file, so the one thing that must not happen
    quietly is the parse coming back empty from a manifest that plainly does
    declare resources — the resource gate would go silent and nothing would
    say so."""
    try:
        return "resources:" in (_root(repo_root) / PANEL_PACKAGE).read_text(encoding="utf-8")
    except OSError:
        return False


def _resource_files(repo_root: Path) -> Iterable[Path]:
    """Every declared resource, dotfiles excluded at any depth — those are the
    operating system's litter, not the panel's art, and SwiftPM's copy of a
    directory carries whatever was there when it ran."""
    files: List[Path] = []
    for base in panel_resource_dirs(repo_root):
        for p in base.rglob("*"):
            # Relative to the resource directory: the root's own path may hold
            # a dot component and says nothing about the file.
            if p.is_file() and not any(
                    part.startswith(".") for part in p.relative_to(base).parts):
                files.append(p)
    return sorted(files)


def panel_freshness(repo_root, binary=None, bundle=None) -> Verdict:
    """Refuse a panel binary that is missing, older than any linked source, or
    whose resource bundle is missing, incomplete, or older than the source
    resources. Equal mtimes pass: SwiftPM's resource copy preserves them."""
    root = _root(repo_root)
    binary = Path(binary) if binary else root / PANEL_BINARY
    bundle = Path(bundle) if bundle else root / PANEL_BUNDLE

    if not binary.is_file():
        return Verdict(False, f"panel binary missing: {binary}", str(binary))
    binary_mtime = binary.stat().st_mtime

    newest: Optional[Path] = None
    newest_mtime = float("-inf")
    for source in panel_linked_sources(root):
        mtime = source.stat().st_mtime
        if mtime > newest_mtime:
            newest, newest_mtime = source, mtime
    if newest is not None and newest_mtime > binary_mtime:
        return Verdict(
            False,
            f"panel binary is older than its source: {newest} "
            f"(source {newest_mtime:.3f} > binary {binary_mtime:.3f}); {STALE_ADVICE}",
            str(newest),
        )

    if not bundle.is_dir():
        return Verdict(False, f"panel resource bundle missing: {bundle}", str(bundle))

    declared = panel_resource_dirs(root)
    if not declared and manifest_declares_resources(root):
        return Verdict(
            False,
            f"panel resources could not be read out of {root / PANEL_PACKAGE}: the "
            "manifest declares resources but none resolved, so the bundle went "
            "unchecked; teach panel_resource_dirs the new form",
            str(root / PANEL_PACKAGE),
        )

    resource_root = root / PANEL_RESOURCE_DIR
    for source in _resource_files(root):
        rel = source.relative_to(resource_root)
        copy = bundle / rel
        if not copy.is_file():
            return Verdict(False, f"panel resource absent from the bundle: {rel}", str(source))
        src_stat, copy_stat = source.stat(), copy.stat()
        if src_stat.st_size != copy_stat.st_size:
            return Verdict(
                False,
                f"panel resource differs in size from its bundle copy: {rel} "
                f"(source {src_stat.st_size} B, bundle {copy_stat.st_size} B)",
                str(source),
            )
        if src_stat.st_mtime > copy_stat.st_mtime:
            return Verdict(
                False,
                f"panel resource is newer than its bundle copy: {rel} "
                f"(source {src_stat.st_mtime:.3f} > bundle {copy_stat.st_mtime:.3f})",
                str(source),
            )
    return Verdict(True, "panel is fresh", str(binary))


# --- extension -------------------------------------------------------------


def package_version(package_json) -> Verdict:
    """The ``version`` the extension manifest declares, or a refusal."""
    path = Path(package_json)
    if not path.is_file():
        return Verdict(False, f"extension manifest missing: {path}", str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Verdict(False, f"extension manifest unreadable: {path} ({exc})", str(path))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        return Verdict(False, f"extension manifest declares no version: {path}", str(path))
    return Verdict(True, version.strip(), str(path))


def archive_version(vsix) -> str:
    """The ``version`` inside the package's own ``extension/package.json``, or
    ``""`` on any failure — a package that cannot say its version matches
    nothing."""
    try:
        with zipfile.ZipFile(vsix) as archive:
            data = json.loads(archive.read("extension/package.json").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return ""
    version = data.get("version") if isinstance(data, dict) else None
    return version.strip() if isinstance(version, str) else ""


def _other_versions(ext_dir: Path, candidate: Path) -> List[str]:
    found = []
    for path in ext_dir.glob(f"{VSIX_PREFIX}*.vsix"):
        if path == candidate:
            continue
        match = _VSIX_RE.search(path.name)
        if match:
            found.append(match.group(1))
    return sorted(found, key=lambda v: tuple(int(n) for n in v.split(".")))


def vsix_match(ext_dir, built_since: Optional[float] = None) -> Verdict:
    """The one package that may ship: ``dark-army-ide-<manifest version>.vsix``
    in ``ext_dir``, carrying that same version inside, and — when
    ``built_since`` is given — written no earlier than that epoch second."""
    ext_dir = Path(ext_dir)
    manifest = package_version(ext_dir / "package.json")
    if not manifest.ok:
        return manifest
    version = manifest.reason
    candidate = ext_dir / f"{VSIX_PREFIX}{version}.vsix"
    if not candidate.is_file():
        others = _other_versions(ext_dir, candidate)
        present = ", ".join(others) if others else "none"
        return Verdict(
            False,
            f"no package for manifest version {version}: expected {candidate.name}; "
            f"packages present: {present}",
            str(candidate),
        )
    inside = archive_version(candidate)
    if inside != version:
        return Verdict(
            False,
            f"{candidate.name} carries version {inside or 'unknown'} inside, "
            f"manifest says {version}",
            str(candidate),
        )
    if built_since is not None and candidate.stat().st_mtime < built_since:
        return Verdict(
            False,
            f"{candidate.name} was not built by this run "
            f"(mtime {candidate.stat().st_mtime:.0f} < build start {built_since:.0f})",
            str(candidate),
        )
    return Verdict(True, f"extension {candidate.name} matches manifest {version}",
                   str(candidate.resolve()))


# --- lockfile --------------------------------------------------------------

LOCKFILE = "package-lock.json"


def lockfile_digest(path) -> str:
    """The bare lowercase sha256 of a file's bytes, or ``""`` when it cannot be
    read. It goes in the manifest so two bundles can be compared without
    shipping the lockfile itself."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


_LOCK_FIX = "run `cd vscode-extension && npm install` and commit the lockfile"


def _declared_ranges(entry) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(entry, dict):
        return out
    for key in ("dependencies", "devDependencies"):
        block = entry.get(key)
        if isinstance(block, dict):
            for name, spec in block.items():
                out[f"{key}:{name}"] = spec if isinstance(spec, str) else ""
    return out


def lock_match(ext_dir) -> Verdict:
    """Refuse a lockfile that no longer describes the manifest beside it.

    ``npm ci`` refuses the same disagreement, but in npm's words and after a
    wall of installer output; this runs first so the build says which two files
    disagree and what to run."""
    ext_dir = Path(ext_dir)
    lock_path = ext_dir / LOCKFILE
    if not lock_path.is_file():
        return Verdict(False,
                       f"extension lockfile missing: {lock_path}; {_LOCK_FIX}",
                       str(lock_path))
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Verdict(False,
                       f"extension lockfile unreadable: {lock_path} ({exc}); {_LOCK_FIX}",
                       str(lock_path))
    if not isinstance(lock, dict):
        return Verdict(False,
                       f"extension lockfile is not an object: {lock_path}; {_LOCK_FIX}",
                       str(lock_path))
    version_of = lock.get("lockfileVersion")
    if not isinstance(version_of, int) or version_of < 2:
        return Verdict(
            False,
            f"extension lockfile is version {version_of!r}: npm ci needs "
            f"lockfileVersion 2 or newer ({lock_path}); {_LOCK_FIX}",
            str(lock_path),
        )
    packages = lock.get("packages")
    root = packages.get("") if isinstance(packages, dict) else None
    if not isinstance(root, dict):
        return Verdict(
            False,
            f'extension lockfile has no root packages[""] entry: {lock_path}; {_LOCK_FIX}',
            str(lock_path),
        )

    manifest = package_version(ext_dir / "package.json")
    if not manifest.ok:
        return manifest
    version = manifest.reason
    lock_version = lock.get("version")
    if lock_version != version:
        return Verdict(
            False,
            f"extension lockfile records version {lock_version or 'none'}, "
            f"manifest says {version}; {_LOCK_FIX}",
            str(lock_path),
        )

    try:
        declared = json.loads((ext_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Verdict(False, f"extension manifest unreadable: {ext_dir / 'package.json'} ({exc})",
                       str(ext_dir / "package.json"))
    want = _declared_ranges(declared)
    have = _declared_ranges(root)
    if want != have:
        missing = sorted(k for k in want if k not in have)
        extra = sorted(k for k in have if k not in want)
        changed = sorted(k for k in want if k in have and want[k] != have[k])
        parts = []
        if missing:
            parts.append("not in the lockfile: " + ", ".join(missing))
        if extra:
            parts.append("only in the lockfile: " + ", ".join(extra))
        if changed:
            parts.append("different range: " + ", ".join(
                f"{k} ({want[k]} vs {have[k]})" for k in changed))
        return Verdict(
            False,
            "extension lockfile and manifest disagree — " + "; ".join(parts)
            + f"; {_LOCK_FIX}",
            str(lock_path),
        )
    return Verdict(True, f"lockfile matches manifest {version}", str(lock_path.resolve()))


# --- manifest --------------------------------------------------------------

MANIFEST_NAME = "build-manifest.json"
MANIFEST_SCHEMA = 1

_VERSION_INFO = Path("host") / "dark_army_menubar" / "_version_info.py"
_VERSION_RE = re.compile(r'^VERSION\s*=\s*"([^"]*)"', re.MULTILINE)


def _tool_version(argv: Sequence[str]) -> str:
    """The first line a tool prints when asked its version, or ``""``. Every
    failure resolves to the empty string and every probe carries a timeout: a
    provenance note must never be able to hang or fail a build."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    # `swift --version` prints its banner on stderr on some toolchains.
    for stream in (done.stdout or "", done.stderr or ""):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()
    return ""


def _app_version(repo_root) -> str:
    """The version py2app baked into ``_version_info.py``, read with a regex
    and **never** imported — this module is stdlib-only and must stay so."""
    try:
        text = (_root(repo_root) / _VERSION_INFO).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def build_manifest(repo_root, ext_dir, vsix=None, mode="strict",
                   extension_source="built") -> dict:
    """What this bundle was actually built from. Every key is always present;
    anything unavailable is ``""`` rather than missing, so a reader never has
    to tell absent from unknown."""
    ext_dir = Path(ext_dir)
    manifest = package_version(ext_dir / "package.json")
    return {
        "schema": MANIFEST_SCHEMA,
        "built_at": datetime.now(timezone.utc).replace(microsecond=0)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "app_version": _app_version(repo_root),
        "extension": {
            "version": manifest.reason if manifest.ok else "",
            "vsix": Path(vsix).name if vsix else "",
            "source": extension_source,
            "lockfile_sha256": lockfile_digest(ext_dir / LOCKFILE),
        },
        "tools": {
            "node": _tool_version(["node", "--version"]),
            "npm": _tool_version(["npm", "--version"]),
            "swift": _tool_version(["swift", "--version"]),
            "python": sys.version.split()[0],
        },
    }


def write_manifest(out_path, manifest: dict) -> None:
    """Written by :mod:`json`, never by a shell heredoc: a checkout path or a
    tool banner carrying a quote would otherwise produce invalid JSON."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- release ---------------------------------------------------------------
#
# The release layer answers one question the freshness gates never could: *what
# number is this build, and is it entitled to one?* The git tag is the single
# source of truth for the Mac app; the extension and the phone keep their own
# independent numbers and are only checked for internal consistency and
# recorded. Everything here is stdlib, and none of it is imported by the
# runtime — `build.sh` calls it as a subprocess and `setup.py` imports it in a
# process that never becomes the app.

EXT_MANIFEST = Path("vscode-extension") / "package.json"
IOS_PBXPROJ = Path("ios") / "BobPhone.xcodeproj" / "project.pbxproj"

RELEASE_MANIFEST_NAME = "release-manifest.json"

# `vX.Y.Z` or `X.Y.Z` and nothing else. A pre-release suffix (`v1.2.3-rc1`) and
# a `-dirty` tail both fail on purpose: see `plist_version`.
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_MARKETING_RE = re.compile(r"MARKETING_VERSION = ([^;\n]+);")
_CURRENT_PROJECT_RE = re.compile(r"CURRENT_PROJECT_VERSION = ([^;\n]+);")

_GIT_TIMEOUT = 5


def _git(repo_root, *args):
    """One git call, or ``None`` when git could not answer at all (absent,
    timed out, or the directory does not exist). ``None`` is deliberately
    distinct from a non-zero return: the callers treat "git could not answer"
    and "git said no" differently."""
    try:
        return subprocess.run(["git", *args], cwd=str(repo_root),
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _is_checkout(repo_root) -> bool:
    done = _git(repo_root, "rev-parse", "--is-inside-work-tree")
    return done is not None and done.returncode == 0 and done.stdout.strip() == "true"


def is_dirty(repo_root) -> Optional[bool]:
    """Whether the work tree has uncommitted changes, or ``None`` when git
    could not answer. ``None`` is not ``False``: a release gate that read an
    unanswerable tree as clean would be exactly the lie this layer exists to
    stop."""
    done = _git(repo_root, "status", "--porcelain")
    if done is None or done.returncode != 0:
        return None
    return bool(done.stdout.strip())


def dirty_paths(repo_root, limit: int = 5) -> List[str]:
    """The first few paths making the tree dirty, so a refusal is actionable
    rather than merely correct."""
    done = _git(repo_root, "status", "--porcelain")
    if done is None or done.returncode != 0:
        return []
    out = []
    for line in done.stdout.splitlines():
        name = line[3:].strip() if len(line) > 3 else line.strip()
        if name:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def describe_version(repo_root) -> str:
    """The descriptive version string, moved here out of ``host/setup.py``.

    A clean tree on an exact tag gives that tag (``v1.2.3``); a dirty one
    appends ``-dirty``; otherwise ``<branch>+<N>@<sha>[-dirty]``. ``"unknown"``
    when git is absent, times out, or the directory is not a checkout.

    **This is a second implementation of ``version.py:_version_from_git``, on
    purpose.** ``version.py`` runs inside the app and deliberately does not
    import this module — the runtime/build separation
    (`test_build_check.py`'s stdlib-only pin) is worth more than the
    deduplication. The two are held equal by a test on the real checkout
    (`host/tests/test_release_version.py`), and by nothing else: change one and
    change the other. A `/` in a branch name is folded to `-` because this
    string goes in the manifest, which ships in a public zip under a rule that
    no value in it may look like a path; ``version.py`` mirrors that fold, so
    the two stay byte-identical on a slashed branch the agreement pin — which
    runs on whatever branch the checkout is on — would otherwise never see."""
    root = Path(repo_root)
    if not _is_checkout(root):
        return "unknown"
    suffix = "-dirty" if is_dirty(root) else ""
    tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
    if tag is not None and tag.returncode == 0 and tag.stdout.strip():
        # A tag may carry `/` too (`release/1.2.3`). Only reachable under
        # --allow-untagged, since the gate refuses any tag that is not vX.Y.Z,
        # but the manifest's no-path rule is over every value, not most of them.
        return tag.stdout.strip().replace("/", "-") + suffix
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = _git(root, "rev-parse", "--short", "HEAD")
    count = _git(root, "rev-list", "--count", "HEAD", "--not",
                 "--remotes=origin/master", "--remotes=origin/main")
    if count is None or count.returncode != 0:
        count = _git(root, "rev-list", "--count", "HEAD", "^master")
    b = branch.stdout.strip() if branch is not None and branch.returncode == 0 else "unknown"
    # A branch name may contain `/` (`feature/thing`). This string reaches the
    # release manifest, which ships in a public zip under a rule that no value
    # in it may look like a path — so the separator is folded to `-` here
    # rather than defended against downstream. `version.py` carries the same
    # line, or the agreement pin goes blind on a slashed branch.
    b = b.replace("/", "-")
    s = sha.stdout.strip() if sha is not None and sha.returncode == 0 else "??????"
    n = count.stdout.strip() if count is not None and count.returncode == 0 else "?"
    return f"{b}+{n}@{s}{suffix}"


def plist_version(described: str) -> str:
    """The value macOS will accept in ``CFBundleVersion`` and
    ``CFBundleShortVersionString``, derived from the descriptive string.

    ``v1.2.3`` and ``1.2.3`` give ``1.2.3``. **Anything else — a pre-release
    tag, a ``-dirty`` tail, a ``main+0@abc1234`` development string, or
    ``unknown`` — gives ``"0.0.0"``**, because those fields are dot-separated
    digits and smuggling anything else in produces a bundle macOS reads
    wrongly rather than refuses.

    Both keys take the *same* string. There is no App Store submission and no
    Sparkle feed behind this app, so a separate monotonic build counter would
    be a number invented to look like provenance it does not have."""
    match = _TAG_RE.match((described or "").strip())
    if not match:
        return "0.0.0"
    return "{}.{}.{}".format(*match.groups())


def panel_plist_versions(repo_root) -> dict:
    """``{"short": …, "bundle": …}`` from ``panel/Sources/BobPanel/Info.plist``
    — the pair welded into the executable's ``__TEXT,__info_plist`` — or ``{}``
    on any read failure."""
    path = _root(repo_root) / PANEL_INFO_PLIST
    try:
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "short": str(data.get("CFBundleShortVersionString", "")),
        "bundle": str(data.get("CFBundleVersion", "")),
    }


def _pbxproj_text(repo_root) -> str:
    try:
        return (_root(repo_root) / IOS_PBXPROJ).read_text(encoding="utf-8")
    except OSError:
        return ""


def ios_marketing_versions(repo_root) -> List[str]:
    """Every distinct ``MARKETING_VERSION`` the phone project declares, in file
    order. Read-only: this layer never edits the Xcode project."""
    out: List[str] = []
    for raw in _MARKETING_RE.findall(_pbxproj_text(repo_root)):
        value = raw.strip()
        if value and value not in out:
            out.append(value)
    return out


def ios_current_project_versions(repo_root) -> List[str]:
    """Every distinct ``CURRENT_PROJECT_VERSION``, in file order.

    **Recorded, never refused on**: `.github/workflows/testflight.yml`
    legitimately overrides it with the CI run number at archive time, so a
    disagreement between the checked-in value and a shipped build is normal."""
    out: List[str] = []
    for raw in _CURRENT_PROJECT_RE.findall(_pbxproj_text(repo_root)):
        value = raw.strip()
        if value and value not in out:
            out.append(value)
    return out


def release_versions(repo_root, version: Optional[str] = None) -> dict:
    """The one place every component's number is collected. ``version=``
    carries a reading taken earlier, so a caller that ran after the build
    dirtied the tree does not re-judge it."""
    root = _root(repo_root)
    described = version if version else describe_version(root)
    commit = _git(root, "rev-parse", "--short", "HEAD")
    extension = package_version(root / EXT_MANIFEST)
    return {
        "version": described,
        "commit": commit.stdout.strip() if commit is not None and commit.returncode == 0 else "",
        "app": plist_version(described),
        # The refusal's `reason` carries a filesystem path, so only a *good*
        # reading may reach the manifest: it ships in a public zip.
        "extension": extension.reason if extension.ok else "",
        "panel_embedded": panel_plist_versions(root),
        "phone": {
            "marketing": ios_marketing_versions(root),
            "current_project_version": ios_current_project_versions(root),
        },
    }


def release_gate(repo_root, allow_untagged: bool = False) -> Verdict:
    """Whether this tree may produce a numbered build.

    Refuses in this order, each in its own words: not a git checkout; HEAD is
    not on an exact ``vX.Y.Z`` tag; the work tree is dirty; the extension
    manifest's version is not ``X.Y.Z``; the phone declares more than one
    distinct ``MARKETING_VERSION``.

    ``allow_untagged=True`` (``build.sh --dev``) downgrades the first three to
    a note on the passing verdict and keeps the last two: an unnumbered build
    must still be internally consistent, it just has no number.

    The wording of the first three refusals has a second reader: ``build.sh``
    matches ``tag`` / ``work tree`` / ``git checkout`` in a ``case`` to decide
    whether to print the ``--allow-untagged`` hint under a refusal. Rewording
    one of them costs the hint, not correctness — the refusal itself is always
    printed verbatim."""
    root = _root(repo_root)
    notes: List[str] = []

    def refuse(reason: str) -> Optional[Verdict]:
        if not allow_untagged:
            return Verdict(False, reason, str(root))
        notes.append(reason)
        return None

    if not _is_checkout(root):
        bad = refuse(f"{root} is not a git checkout, so there is no tag to take a "
                     "release version from")
        if bad is not None:
            return bad
    else:
        tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
        name = tag.stdout.strip() if tag is not None and tag.returncode == 0 else ""
        if not _TAG_RE.match(name):
            bad = refuse(
                "HEAD is not on an exact vX.Y.Z tag "
                f"({name or 'no tag on HEAD'}), so this build would carry no "
                "release number; tag the release first")
            if bad is not None:
                return bad
        dirty = is_dirty(root)
        if dirty is None:
            bad = refuse("git could not say whether the work tree is clean, so the "
                         "release version cannot be trusted")
            if bad is not None:
                return bad
        elif dirty:
            paths = dirty_paths(root)
            listed = ", ".join(paths) if paths else "see git status"
            bad = refuse("the work tree has uncommitted changes, so the release "
                         f"version would be a lie: {listed}")
            if bad is not None:
                return bad

    extension = package_version(root / EXT_MANIFEST)
    if not extension.ok:
        return Verdict(False, extension.reason, extension.path)
    if not _TAG_RE.match(extension.reason):
        return Verdict(
            False,
            f"the extension manifest declares version {extension.reason!r}, which is "
            "not X.Y.Z; vscode_extension.ensure_installed() compares it numerically",
            extension.path,
        )

    marketing = ios_marketing_versions(root)
    if len(marketing) > 1:
        return Verdict(
            False,
            "the phone project declares more than one MARKETING_VERSION "
            f"({', '.join(marketing)}); they must agree across configurations",
            str(root / IOS_PBXPROJ),
        )

    if notes:
        return Verdict(True, "unnumbered build: " + "; ".join(notes), str(root))
    return Verdict(True, "release version is consistent", str(root))


def write_release_manifest(repo_root, dest, version: Optional[str] = None,
                           panel_version: Optional[str] = None) -> dict:
    """Write ``release-manifest.json`` — the short list of what went into this
    bundle — and return what was written.

    Exactly these keys and no others: ``version``, ``commit``, ``app``,
    ``panel``, ``panel_embedded``, ``extension``, ``phone``. **No timestamps,
    no filesystem paths, no hostname, no user name**: this file ships in a
    public zip, and the whole point of the change it belongs to is that a
    public artifact carries no developer path.

    ``panel_embedded`` records the ``1.0``/``1`` still welded into the panel
    executable's ``__TEXT,__info_plist`` by ``panel/Package.swift``. It does
    **not** match ``panel``, which is what ``build.sh`` writes into the nested
    bundle's own ``Info.plist``; recording the divergence honestly is the point
    — papering over it is the class of bug this layer exists to end.

    ``panel_version=`` is that write, reported by the caller that made it: the
    stamp lives inside ``build.sh``'s "is there a panel binary" branch while
    this call is unconditional, so a ``--dev`` build with no panel would
    otherwise ship a manifest asserting a write that never happened. ``""``
    means no nested bundle was produced; ``None`` (a direct caller with no
    opinion) keeps the app's own pair."""
    info = release_versions(repo_root, version=version)
    app = {"short": info["app"], "bundle": info["app"]}
    if panel_version is None:
        panel = dict(app)
    elif panel_version:
        panel = {"short": panel_version, "bundle": panel_version}
    else:
        panel = {"short": "", "bundle": ""}
    payload = {
        "version": info["version"],
        "commit": info["commit"],
        "app": app,
        # A record of the stamp build.sh actually wrote, not a second answer.
        "panel": panel,
        "panel_embedded": info["panel_embedded"],
        "extension": info["extension"],
        "phone": info["phone"],
    }
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


# --- the artifact names no builder --------------------------------------------

#: The route out of a refusal, named in the refusal itself.
NEUTRAL_CHECKOUT = "/Users/Shared/dark-army-release"


def home_path_hits(app, needle: str) -> Dict[str, int]:
    """Every file under ``app`` whose bytes carry ``needle``, with the count.

    A zip (py2app's ``python312.zip``) is opened and each member searched on
    its own, reported as ``archive!member``: its contents are compressed, so a
    byte search of the archive alone sees none of them. Symlinks are skipped —
    their targets are searched where they live. Measured 24 Sep 2026 on a
    build from the home checkout: the unstripped panel binary names every
    source file (317 each copy), plus ``Info.plist``'s ``PythonExecutable``,
    py2app's ``site.pyc`` and the ``--install`` stamp."""
    root = Path(app)
    raw = needle.encode("utf-8")
    hits: Dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(root))
        data = path.read_bytes()
        if raw in data:
            hits[rel] = data.count(raw)
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        body = archive.read(member)
                        if raw in body:
                            hits[f"{rel}!{member}"] = body.count(raw)
            except (zipfile.BadZipFile, OSError):
                continue
    return hits


def home_path_verdict(app, home: Optional[str] = None) -> Verdict:
    """Refuse a bundle that carries the builder's home folder.

    The needle is the home folder *with* its trailing slash, so
    ``/Users/bob`` does not match ``/Users/bobby``. Third-party wheels carry
    other people's paths (``/Users/runner``) and that is fine: the question is
    whether this machine's owner is named, not whether ``/Users/`` appears."""
    base = (home or str(Path.home())).rstrip("/")
    if not Path(app).is_dir():
        # A build that died before the bundle existed has nothing to scan,
        # and an empty scan must never read as a clean one.
        return Verdict(False, "no bundle to scan", str(app))
    hits = home_path_hits(app, base + "/")
    if not hits:
        return Verdict(True, "no home folder in the bundle", str(app))
    worst = sorted(hits.items(), key=lambda item: (-item[1], item[0]))[:5]
    listed = "; ".join(f"{name} ({count})" for name, count in worst)
    more = f" and {len(hits) - len(worst)} more" if len(hits) > len(worst) else ""
    return Verdict(
        False,
        f"the bundle carries your home folder {base} in {len(hits)} file(s): "
        f"{listed}{more}. Build the release from a checkout outside your home "
        f"folder, e.g. {NEUTRAL_CHECKOUT} with its own host/.venv",
        str(app))


# --- CLI -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build_check")
    sub = parser.add_subparsers(dest="command", required=True)
    panel = sub.add_parser("panel", help="refuse a stale or incomplete panel build")
    panel.add_argument("--repo-root", required=True)
    panel.add_argument("--binary")
    panel.add_argument("--bundle")
    vsix = sub.add_parser("vsix", help="print the one .vsix matching the manifest")
    vsix.add_argument("--ext-dir", required=True)
    vsix.add_argument("--built-since", type=float)
    lock = sub.add_parser("lock", help="refuse a lockfile that no longer matches package.json")
    lock.add_argument("--ext-dir", required=True)
    man = sub.add_parser("manifest", help="write the bundle's provenance note")
    man.add_argument("--out", required=True)
    man.add_argument("--repo-root", required=True)
    man.add_argument("--ext-dir", required=True)
    man.add_argument("--vsix")
    man.add_argument("--mode", choices=("strict", "dev"), default="strict")
    man.add_argument("--extension-source", choices=("built", "committed", "none"),
                     default="built")
    release = sub.add_parser(
        "release", help="decide the release version, or write release-manifest.json")
    release.add_argument("--repo-root", required=True)
    release.add_argument("--allow-untagged", action="store_true")
    release.add_argument("--manifest")
    release.add_argument("--version")
    release.add_argument("--panel-version")
    home = sub.add_parser(
        "home", help="refuse a bundle that carries the builder's home folder")
    home.add_argument("--app", required=True)
    home.add_argument("--home")
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    if args.command == "panel":
        verdict = panel_freshness(args.repo_root, args.binary, args.bundle)
        if verdict.ok:
            return 0
    elif args.command == "lock":
        verdict = lock_match(args.ext_dir)
        if verdict.ok:
            print(verdict.path)
            return 0
    elif args.command == "release":
        verdict = release_gate(args.repo_root, allow_untagged=args.allow_untagged)
        if verdict.ok:
            described = args.version or describe_version(args.repo_root)
            if args.manifest:
                try:
                    write_release_manifest(args.repo_root, args.manifest,
                                           version=described,
                                           panel_version=args.panel_version)
                except OSError as exc:
                    verdict = Verdict(
                        False,
                        f"release manifest not written: {args.manifest} ({exc})",
                        str(args.manifest))
                else:
                    return 0
            else:
                # Exactly two space-separated tokens on one line: neither the
                # descriptive string nor the plist form can contain a space, so
                # build.sh reads them with a bare `read -r a b`.
                print(f"{described} {plist_version(described)}")
                return 0
    elif args.command == "home":
        verdict = home_path_verdict(args.app, args.home)
        if verdict.ok:
            return 0
    elif args.command == "manifest":
        manifest = build_manifest(args.repo_root, args.ext_dir, args.vsix or None,
                                  args.mode, args.extension_source)
        try:
            write_manifest(args.out, manifest)
        except OSError as exc:
            verdict = Verdict(False, f"build manifest not written: {args.out} ({exc})",
                              str(args.out))
        else:
            print(str(Path(args.out).resolve()))
            return 0
    else:
        verdict = vsix_match(args.ext_dir, args.built_since)
        if verdict.ok:
            print(verdict.path)
            return 0
    print(f"build-check: {verdict.reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
