"""The version string Dark Army shows about itself.

A frozen app carries it: py2app's build writes `_version_info.py` beside this
module, holding the string the release gate settled on. A run from the
checkout has no such file and reads the same rule off git instead. The
build's own reading of that rule is a second copy kept on purpose, since the
runtime never imports the build helper; `test_release_version.py` holds the
two equal.
The answer is worked out once per process.
"""

from __future__ import annotations

import functools
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("dark-army.menubar")

# The two failures that mean "git cannot tell us", as opposed to git
# answering with a non-zero exit: no git on PATH, or git hanging.
_NO_ANSWER = (FileNotFoundError, subprocess.TimeoutExpired)


@functools.lru_cache(maxsize=1)
def get_version() -> str:
    """The baked-in version when frozen, else the one git describes."""
    try:
        from . import _version_info
    except ImportError:
        return _version_from_git()
    return _version_info.VERSION


def _git(*argv: str) -> Optional[str]:
    """Stripped stdout of one git command in the process's own directory, or
    None when git ran and refused. Raises the `_NO_ANSWER` errors."""
    done = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=5)
    return done.stdout.strip() if done.returncode == 0 else None


def _slashless(name: str) -> str:
    # The build writes this string into a manifest that ships publicly, under
    # a rule that no value in it may look like a path; tag and branch names
    # may both hold `/`. Folding it here too keeps the two readings
    # byte-identical, which is what the agreement test compares.
    return name.replace("/", "-")


def _version_from_git() -> str:
    """`<tag>[-dirty]` on an exact tag, else `<branch>+<N>@<sha>[-dirty]`,
    where N counts the commits not yet on origin's main line; `"unknown"`
    when git is missing or too slow to answer."""
    try:
        tag = _git("describe", "--tags", "--exact-match", "HEAD")
        if tag:
            head = _slashless(tag)
        else:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD")
            sha = _git("rev-parse", "--short", "HEAD")
            ahead = _git("rev-list", "--count", "HEAD", "--not",
                         "--remotes=origin/master", "--remotes=origin/main")
            if ahead is None:
                ahead = _git("rev-list", "--count", "HEAD", "^master")
            head = "{}+{}@{}".format(
                _slashless("unknown" if branch is None else branch),
                "?" if ahead is None else ahead,
                "??????" if sha is None else sha,
            )
        return head + ("-dirty" if _is_dirty() else "")
    except _NO_ANSWER:
        return "unknown"


def _is_dirty() -> bool:
    """Whether `git status` lists anything at all; False when git cannot say."""
    try:
        status = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True, timeout=5)
    except _NO_ANSWER:
        return False
    return bool(status.stdout.strip())
