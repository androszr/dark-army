"""Install and version-check the ``dark-army-ide`` VS Code extension.

The extension is what lets the daemon focus a session's integrated-terminal tab
(see ``dark_army_daemon.vscode_reveal``). It ships as a prebuilt ``.vsix`` — in a
frozen app under ``Contents/Resources/`` (copied there by ``host/build.sh``, exactly
like the simulator binary), and in a source checkout under ``vscode-extension/``.

Mirrors ``hooks.py``: a version-checked, idempotent install run once at startup, plus a
menu row to (re)install on demand. Installing reloads the extension host of every open
window, so it is done only when the installed version is missing or older — never on
every launch.

The ``code`` CLI is usually not on PATH on macOS (the app bundle ships it unlinked), so
binary discovery reuses ``vscode_reveal.find_code_binary`` (app-bundle path first).
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from dark_army_daemon.vscode_reveal import find_code_binary, reap_stale_code_cli, run_code

logger = logging.getLogger("dark-army.menubar")

EXTENSION_ID = "dark-army.dark-army-ide"
VSIX_GLOB = "dark-army-ide-*.vsix"
_VSIX_RE = re.compile(r"dark-army-ide-(\d+\.\d+\.\d+)\.vsix$")


def _find_vsix() -> Optional[Path]:
    """Locate the bundled .vsix. Frozen bundle Resources first, then a checkout."""
    # 1. Inside the .app bundle (Contents/Resources/), where build.sh copies it.
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        if bundle:
            res = Path(bundle.bundlePath()) / "Contents" / "Resources"
            hit = _best_vsix(res)
            if hit:
                return hit
    except ImportError:
        pass
    # 2. Next to sys.executable (non-bundle fallback).
    hit = _best_vsix(Path(sys.executable).parent)
    if hit:
        return hit
    # 3. A source checkout: repo/vscode-extension/. This file is
    #    host/dark_army_menubar/vscode_extension.py → repo root is three up.
    here = Path(os.path.realpath(__file__))
    for _ in range(3):
        here = here.parent
    hit = _best_vsix(here / "vscode-extension")
    return hit


def _best_vsix(directory: Path) -> Optional[Path]:
    """The highest-versioned .vsix in `directory`, or None.

    By version, never by mtime. Nothing prunes old .vsix files, so a checkout keeps
    every one it has ever built; `git clone`/`git checkout` then stamp them all with
    checkout time in arbitrary order. Picking the newest mtime there can hand back
    0.1.1 while 0.2.0 sits beside it — and `install_extension` passes `--force`, so
    the user who clicked the menu row gets silently *downgraded*.
    """
    try:
        cands = [p for p in directory.glob(VSIX_GLOB) if _VSIX_RE.search(p.name)]
    except OSError:
        return None
    if not cands:
        return None
    return max(cands, key=lambda p: _parse_version(_VSIX_RE.search(p.name).group(1)))


def _parse_version(s: str) -> tuple:
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (0,)


def bundled_version() -> Optional[str]:
    vsix = _find_vsix()
    if not vsix:
        return None
    m = _VSIX_RE.search(vsix.name)
    return m.group(1) if m else None


def installed_version(extension_id: str = EXTENSION_ID) -> Optional[str]:
    """An installed extension's version via `code --list-extensions`, or None."""
    r = run_code("--list-extensions", "--show-versions", timeout=15)
    if r is None:
        return None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith(extension_id + "@"):
            return line.split("@", 1)[1]
    return None


def is_installed() -> bool:
    return installed_version() is not None


def install_extension() -> tuple[bool, str]:
    """Install (or upgrade) the bundled .vsix via `code --install-extension --force`.

    Returns (ok, detail). Installing reloads open windows' extension hosts, so the
    extension only becomes live in windows opened or reloaded afterwards — surfaced
    in the detail string for the menu action."""
    if not find_code_binary():
        return False, "VS Code CLI not found"
    vsix = _find_vsix()
    if not vsix:
        return False, "bundled extension (.vsix) not found"
    r = run_code("--install-extension", str(vsix), "--force", timeout=90)
    if r is None:
        return False, "install failed: timeout"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "install failed").strip()[:200]
    logger.info("Installed VS Code extension %s from %s", bundled_version(), vsix.name)
    return True, "installed (reload a VS Code window to activate)"


#: `install_extension` details that are safe to show a person verbatim.
#: Anything else is the CLI's output and is summarised instead.
_SAFE_FAILURES = frozenset({
    "VS Code CLI not found",
    "bundled extension (.vsix) not found",
    "install failed: timeout",
})


class EnsureResult(NamedTuple):
    """What `ensure_installed()` found and did, in `launch_report`'s
    vocabulary: `changed` (installed or upgraded), `unchanged` (current or
    newer already there), `skipped` (nothing bundled to install), `failed`
    (no CLI, a timeout, a non-zero install) or `unknown` (the check itself
    raised). `detail` is `install_extension`'s own sentence where there is
    one, so a successful install still says a window reload may be needed."""

    status: str
    detail: str


def ensure_installed(report: Optional[Callable[[str, str], None]] = None) -> EnsureResult:
    """Install the extension iff it is missing or older than the bundled one.

    Called once at startup (off the main thread — `code` is a subprocess and the
    install reloads extension hosts). Never blocks or raises; logs and returns.
    The decision — missing or older only, never a downgrade — is unchanged;
    what is new is that the actual result is returned, and handed to
    `report(status, detail)` when a caller passes one, so the launch line
    can say what happened rather than what was attempted."""
    result = _ensure_installed()
    if report is not None:
        try:
            report(result.status, result.detail)
        except Exception:
            logger.debug("VS Code extension result callback failed", exc_info=True)
    return result


def _ensure_installed() -> EnsureResult:
    try:
        reap_stale_code_cli()
        want = bundled_version()
        if not want:
            logger.info("VS Code extension: no bundled .vsix found, skipping install")
            return EnsureResult("skipped", "no bundled editor extension")
        have = installed_version()
        if have is not None and _parse_version(have) >= _parse_version(want):
            logger.debug("VS Code extension up to date (%s)", have)
            return EnsureResult("unchanged", f"editor extension {have} current")
        logger.info("VS Code extension: installing %s (had %s)", want, have)
        ok, detail = install_extension()
        if ok:
            return EnsureResult("changed", f"editor extension {want} {detail}")
        # The menu row may show the CLI's own stderr; the launch line may
        # not — it is drawn beside a newcomer's first checklist, and a
        # traceback there is noise at best and a home directory at worst.
        if detail not in _SAFE_FAILURES:
            detail = "the install command failed"
        return EnsureResult("failed", f"editor extension not installed: {detail}")
    except Exception:  # never let this take down startup
        logger.warning("VS Code extension auto-install failed", exc_info=True)
        return EnsureResult("unknown", "editor extension check failed")
