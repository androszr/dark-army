#!/usr/bin/env python3
"""Copy the canonical ``/review`` skill onto its twin and the personal mirror.

``.claude/skills/review/SKILL.md`` is the one text. Two byte copies exist:
``.agents/skills/review/SKILL.md`` (Codex and Grok read the ``.agents`` tree)
and ``~/.claude/skills/review/SKILL.md`` (the personal mirror two other repos
on this machine rely on). This script is run by hand from a normal terminal
because the managed coding sandbox can read ``.agents`` but cannot write it,
and the personal folder is outside the repo altogether.

The write is idempotent and atomic: a target already in step is not touched
(its mtime does not move), and a differing one is replaced through a
temporary file and ``os.replace``.

``--check`` writes nothing and exits 1 only when the in-repo twin has
drifted. The personal mirror is reported, never enforced: a machine without
it is not out of step, and ``host/tests/test_review_skill.py`` warns rather
than fails on it for the same reason.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / ".claude/skills/review/SKILL.md"
TWIN = ROOT / ".agents/skills/review/SKILL.md"
PERSONAL = Path.home() / ".claude/skills/review/SKILL.md"

IN_STEP = "in step"
UPDATED = "updated"
MISSING = "missing"


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one ``os.replace``.

    Bytes in, bytes out: a text round-trip would fold a CRLF canonical into
    an LF twin that ``sync`` then reports in step while the byte comparison
    in ``test_review_skill.py`` fails forever. The mode is preserved when the
    file exists and ``0o644`` when it does not; the parent directory is
    created on demand.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def sync(canonical: Path, targets: list[Path], *, write: bool) -> dict[Path, str]:
    """Compare every target with ``canonical`` and, when asked, repair it.

    Returns one of ``"in step"``, ``"updated"`` or ``"missing"`` per target.
    ``"missing"`` is what a target that does not exist reads as when
    ``write`` is false; with ``write`` true it is created and reads
    ``"updated"``. ``canonical`` is read exactly once, as bytes, and compared
    and written as bytes, so its line endings are copied rather than folded.
    """
    payload = canonical.read_bytes()
    result: dict[Path, str] = {}
    for target in targets:
        if not target.exists():
            state = MISSING
        elif target.read_bytes() == payload:
            state = IN_STEP
        else:
            state = UPDATED
        if write and state != IN_STEP:
            atomic_write(target, payload)
            state = UPDATED
        result[target] = state
    return result


def run_contract_test() -> int:
    pytest = ROOT / "host/.venv/bin/pytest"
    if not pytest.exists():
        print(f"Updated, but skipped test: {pytest} does not exist")
        return 0
    command = [str(pytest), "-q", "tests/test_review_skill.py"]
    return subprocess.run(command, cwd=ROOT / "host", check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report drift without writing"
    )
    parser.add_argument(
        "--no-personal",
        action="store_true",
        help="leave ~/.claude/skills/review alone (a machine that is not this one)",
    )
    args = parser.parse_args(argv)

    if not CANONICAL.exists():
        print(f"REFUSED: canonical skill missing at {CANONICAL}")
        return 2

    targets = [TWIN] if args.no_personal else [TWIN, PERSONAL]

    if args.check:
        states = sync(CANONICAL, targets, write=False)
        code = 0
        for target, state in states.items():
            if target == PERSONAL:
                if state == IN_STEP:
                    print(f"personal mirror: in step ({target})")
                else:
                    print(
                        f"personal mirror: {state} ({target}) — "
                        "run tools/sync_review_skill.py to refresh it"
                    )
                continue
            if state == IN_STEP:
                print(f"In step: {target}")
            else:
                print(f"OUT OF STEP: {target} is {state}")
                code = 1
        return code

    states = sync(CANONICAL, targets, write=True)
    for target, state in states.items():
        if state == IN_STEP:
            print(f"Already in step: {target}")
        else:
            print(f"Updated {target}")
    return run_contract_test()


if __name__ == "__main__":
    sys.exit(main())
