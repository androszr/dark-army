# host/dark_army_menubar/dev_build.py
"""Build-staleness detection and rebuild helper for the selected panel.

The app supplies the executable saved by its PanelProcess launcher. Compare
that file's current on-disk mtime with Swift source; frozen bundles also include
Python and icon sources because those were copied at build time. Status reaches
the panel's settings and app log, with the existing one-click rebuild action.

Without a located checkout or selected artifact there is nothing to compare.
Replacing that file updates this reading, but an already open panel needs the
existing successful-rebuild restart to load the replacement.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from dark_army_daemon.subprocess_env import (
    PY2APP_ENV_VARS as _PY2APP_ENV_VARS,
    clean_env as _clean_env,
)

# Source trees whose newest file defines "current source". Split so dev mode can
# skip the Python menu-bar code (it runs live from source, never stale), while a
# frozen .app must include it (the bundle froze a copy).
_SWIFT_SOURCES = [("panel/Sources/BobPanel", ("*.swift",))]
_ICON_SOURCES = [("host/dark_army_menubar/icons", ("*.png",))]
_PY_SOURCES = [
    ("host/dark_army_menubar", ("*.py",)),
    ("host/dark_army_daemon", ("*.py",)),
    # Vendored pack tree: markdown, JSON, scripts. Frozen copies it through
    # setup.py resources, so an edit here must mark the bundle stale.
    ("host/dark_army_menubar/agent_pack", ("**/*",)),
]


def is_frozen() -> bool:
    """True when running inside a py2app `.app` bundle.

    `sys.frozen` alone is not enough, and the gap is not theoretical: only the
    bundle's `__boot__.py` sets it, and `_on_restart` relaunches the app as
    `sys.executable -m dark_army_menubar` — the bundled interpreter, booted
    *without* that script. So every run after the first Restart executes from
    the bundle with no `sys.frozen`, and `rebuild()` took the dev branch: it ran
    `cmake --build simulator/build`, rebuilding a binary the bundle never runs,
    returned 0, and the app reported "Rebuild succeeded" while the .app kept its
    old simulator and its old menu-bar icons. Where this module lives says it
    unambiguously — inside a bundle it is under `<app>.app/Contents/Resources/`.
    """
    if getattr(sys, "frozen", False):
        return True
    return ".app/Contents/" in str(Path(__file__).resolve())


# Written into the bundle by build.sh, read back by find_repo_root(). See below.
REPO_ROOT_STAMP = "repo-root"


def bundle_path() -> Optional[Path]:
    """The `.app` this code is executing from, or None outside a bundle.

    Derived from `__file__` first and asked of CFBundle only as a fallback: our
    own location is the authoritative answer (it is what `is_frozen` reads), and
    `NSBundle.mainBundle()` answers `Python.framework/…/Resources/Python.app` for
    a plain interpreter — a real bundle, just not ours."""
    here = str(Path(__file__).resolve())
    marker = ".app/Contents/"
    if marker in here:
        return Path(here[: here.index(marker) + len(".app")])
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        path = bundle.bundlePath() if bundle else None
        if path and str(path).endswith(".app"):
            return Path(str(path))
    except Exception:
        pass
    return None


def _stamped_repo_root() -> Optional[Path]:
    """The checkout this bundle was built from, per the stamp build.sh leaves in
    `Contents/Resources/repo-root`.

    Why a stamp at all: the ancestor walk below only finds source for a checkout
    or a bundle sitting *inside* the repo, and the normal way to run this app is
    `./build.sh --install`, i.e. from /Applications — which has no repo ancestor.
    That silently withdrew the build row and the Rebuild button from exactly the
    install everyone uses, leaving the terminal as the only way to pick up a
    change.

    The path is *verified*, not trusted: a released .app carries whatever path it
    was built on, and on someone else's machine that directory is absent (or,
    worse, something unrelated). No `host/build.sh` there, no repo."""
    if not is_frozen():
        return None
    bundle = bundle_path()
    if bundle is None:
        return None
    stamp = bundle / "Contents" / "Resources" / REPO_ROOT_STAMP
    try:
        root = Path(stamp.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    if root.is_absolute() and (root / "host" / "build.sh").is_file():
        return main_checkout(root)
    return None


def main_checkout(root: Path) -> Path:
    """The main checkout `root` belongs to when it is a linked git worktree — a
    board card's side folder under `.worktrees/` — otherwise `root` unchanged.

    A side folder is temporary (Done removes it), so it is never Dark Army's own
    checkout: an app installed from one enrolled the side folder as a project of
    its own at launch, and every session inside it split off under the side
    folder's name. Read from the files git leaves (`.git` is a file naming the
    worktree's git folder, whose `commondir` names the shared one), never by
    running git. Anything unexpected answers `root`, as before."""
    try:
        text = (root / ".git").read_text(encoding="utf-8")
    except OSError:  # a directory (the main checkout itself) or nothing at all
        return root
    if not text.startswith("gitdir:"):
        return root
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    try:
        common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = common.resolve()
    except OSError:
        return root
    main = common.parent
    if common.name == ".git" and (main / "host" / "build.sh").is_file():
        return main
    return root


def find_repo_root() -> Optional[Path]:
    """Walk up from this file to the repo root (marked by host/build.sh).

    Works in dev (source tree) and for a `.app` that lives *inside* the repo
    (e.g. host/dist/Dark Army.app — the bundle path's ancestors include the
    repo), and for an installed copy via the stamp above. Returns None for an
    installed release with no source anywhere."""
    here = Path(__file__).resolve()
    for base in [here, *here.parents]:
        if (base / "host" / "build.sh").is_file():
            return base
    return _stamped_repo_root()


def _newest_mtime(root: Path, groups) -> float:
    newest = 0.0
    for rel, pats in groups:
        d = root / rel
        if not d.is_dir():
            continue
        # pathlib glob does not descend into dot directories, and the
        # vendored pack lives almost entirely under .claude/.codex/.grok.
        if any("**" in pat for pat in pats):
            for dirpath, _dirnames, filenames in os.walk(d):
                for name in filenames:
                    try:
                        newest = max(
                            newest,
                            os.path.getmtime(os.path.join(dirpath, name)))
                    except OSError:
                        pass
            continue
        for pat in pats:
            for f in d.glob(pat):
                try:
                    newest = max(newest, f.stat().st_mtime)
                except OSError:
                    pass
    return newest


def check_staleness(repo_root: Optional[Path],
                    panel_binary: Optional[str]) -> Optional[dict]:
    """Compare the selected on-disk artifact's mtime with relevant source.

    Returns None when there's nothing to check (no repo source, or no artifact).
    Otherwise: {stale, artifact_mtime, source_mtime, panel_binary}. Frozen mode
    includes Python/icon sources; checkout mode includes only Swift sources.
    This does not identify the bytes already loaded by a running panel.
    """
    if repo_root is None:
        return None
    if not panel_binary or not os.path.isfile(panel_binary):
        return None
    try:
        artifact = os.path.getmtime(panel_binary)
    except OSError:
        return None

    # In a checkout the only thing that needs compiling is the panel, so only
    # its own sources can make it stale. Icons and Python used to count because
    # the artifact was the simulator, which baked icons into sprite headers and
    # was bundled beside the Python — neither is true now, and leaving them in
    # made the row read "stale" permanently after any icon rebake, which trains
    # people to ignore it. When frozen they count again: py2app snapshots both.
    groups = list(_SWIFT_SOURCES)
    if is_frozen():
        groups += list(_ICON_SOURCES) + list(_PY_SOURCES)
    source = _newest_mtime(repo_root, groups)
    return {
        "stale": source > artifact,
        "artifact_mtime": artifact,
        "source_mtime": source,
        "panel_binary": panel_binary,
    }


def artifact_label(info: Optional[dict]) -> str:
    """Short menu line describing build freshness."""
    if info is None:
        return "Build: release (no source)"
    when = time.strftime("%b %d %H:%M", time.localtime(info["artifact_mtime"]))
    if info["stale"]:
        return f"⚠ Build stale (built {when}) — Rebuild"
    return f"Build: up to date ({when})"


def deploys_on_rebuild(repo_root: Optional[Path]) -> bool:
    """True when a rebuild must also *install*, because the bundle we are running
    is not the one `build.sh` produces in `host/dist`.

    That is the /Applications copy, and without `--install` a rebuild there did
    the whole 15-minute build and then changed nothing the user could see: the
    new bundle landed in `dist/`, the restart relaunched the old one from
    /Applications, and the build row went straight back to "stale"."""
    if repo_root is None or not is_frozen():
        return False
    bundle = bundle_path()
    if bundle is None:
        return True
    try:
        bundle.resolve().relative_to(repo_root.resolve())
        return False
    except ValueError:
        return True


def rebuild_title(repo_root: Optional[Path], suffix: str = "") -> str:
    """Menu label for the rebuild verb — it has to say which of the two it does,
    since one of them replaces /Applications underneath the user."""
    verb = "Rebuild & Deploy" if deploys_on_rebuild(repo_root) else "Rebuild & Reload"
    return f"{verb}{suffix}"


def rebuild(repo_root: Path) -> subprocess.CompletedProcess:
    """Rebuild the artifact the running mode actually uses.

    Frozen .app  -> host/build.sh (rebuild the panel + repackage the bundle),
                    with `--install` when we are running from an installed copy
                    rather than from `host/dist` (see deploys_on_rebuild).
    Dev checkout -> swift build in panel/ (the binary the menu bar launches).
    Raises subprocess.TimeoutExpired / OSError on failure to launch.

    In a checkout the menu-bar Python runs live, so the only thing that needs
    compiling is the panel. This used to run `cmake --build` in `simulator/`, and
    kept doing so after that directory was deleted — the rebuild then failed with
    a bare FileNotFoundError, which is exactly the row a person uses to pick up
    new code.

    The decoding is named rather than left to the locale. `text=True` alone
    decodes with `locale.getpreferredencoding()`, and an .app launched from
    Finder or at login inherits no `LANG` — so that is **ASCII**, while
    `build.sh` prints em dashes and check marks. The rebuild ran the whole build
    and then died reading its own output (`'ascii' codec can't decode byte
    0xe2`), which from the menu looks exactly like the button doing nothing.
    `errors="replace"` because this text is only ever shown in a failure alert:
    a mangled character must not cost the message that carries it."""
    if is_frozen():
        # `--allow-untagged`, and deliberately not `--dev`. A checkout being
        # worked on is untagged and dirty every ordinary day, and the version
        # gate at the top of build.sh refuses both — so without this the button
        # dies in about a second with "Rebuild failed", which is precisely the
        # silent no-op this function was written to end. `--dev` would get past
        # it too, and would also reinstate the warn-and-continue fallbacks that
        # let a stale panel or a mismatched extension ship: those gates stay
        # strict here.
        cmd = ["./build.sh", "--allow-untagged"]
        if deploys_on_rebuild(repo_root):
            cmd.append("--install")
        return subprocess.run(
            cmd,
            cwd=str(repo_root / "host"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=_clean_env(),
        )
    return subprocess.run(
        ["swift", "build", "-c", "release"],
        cwd=str(repo_root / "panel"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
