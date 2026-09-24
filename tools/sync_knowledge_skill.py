#!/usr/bin/env python3
"""Copy the shipped ``/knowledge`` skill onto this repo's own four copies.

``sync_review_skill.py``'s shape, with the canonical file the other way round.
For ``/review`` the repo's copy is the original and the pack vendors it; here
the **template** pair is canonical —

    host/dark_army_menubar/agent_pack/template/.claude/skills/knowledge/
        SKILL.md
        questions.md

— because that is the text every other project receives, and the success
criterion this exists to check is that a second project installs *the same
text without modification*. The four targets are this repo's copies:

    .claude/skills/knowledge/SKILL.md
    .claude/skills/knowledge/questions.md
    .agents/skills/knowledge/SKILL.md        (Codex and Grok read `.agents`)
    .agents/skills/knowledge/questions.md

Dark Army's own checkout is refused as a pack destination
(``pack_install._is_bobs_own``), so nothing installs these for us — this script
is that install, and it is why the copies can drift at all.

The write is idempotent and atomic: a target already in step is not touched
(its mtime does not move), and a differing one is replaced through a temporary
file and ``os.replace``. ``--check`` writes nothing and exits 1 naming every
drifted file.

**This is not the seed-once rule.** A *project* that already has the file keeps
it for ever (``pack_install.SEED_ONCE_KEYS``); this repo's four copies are held
byte-identical on purpose, because a drifted copy here would mean the text we
ship and the text we run are two different texts.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "host/dark_army_menubar/agent_pack/template"
            / ".claude/skills/knowledge")

#: canonical -> the copies that must equal it, byte for byte.
PAIRS: dict[Path, list[Path]] = {
    TEMPLATE / "SKILL.md": [
        ROOT / ".claude/skills/knowledge/SKILL.md",
        ROOT / ".agents/skills/knowledge/SKILL.md",
    ],
    TEMPLATE / "questions.md": [
        ROOT / ".claude/skills/knowledge/questions.md",
        ROOT / ".agents/skills/knowledge/questions.md",
    ],
}

IN_STEP = "in step"
UPDATED = "updated"
MISSING = "missing"


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one ``os.replace``.

    Bytes in, bytes out: a text round-trip would fold a CRLF canonical into an
    LF copy that ``sync`` then reports in step while the byte comparison in
    ``host/tests/test_knowledge_skill.py`` fails for ever. The mode is
    preserved when the file exists and ``0o644`` when it does not; the parent
    directory is created on demand.
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


def sync(canonical: Path, targets: list[Path], *, write: bool) -> dict:
    """Compare every target with ``canonical`` and, when asked, repair it.

    Returns one of ``"in step"``, ``"updated"`` or ``"missing"`` per target.
    ``"missing"`` is what a target that does not exist reads as when ``write``
    is false; with ``write`` true it is created and reads ``"updated"``.
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
    command = [str(pytest), "-q", "tests/test_knowledge_skill.py"]
    return subprocess.run(command, cwd=ROOT / "host", check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report drift without writing"
    )
    args = parser.parse_args(argv)

    for canonical in PAIRS:
        if not canonical.exists():
            print(f"REFUSED: canonical skill missing at {canonical}")
            return 2

    if args.check:
        code = 0
        for canonical, targets in PAIRS.items():
            for target, state in sync(canonical, targets, write=False).items():
                if state == IN_STEP:
                    print(f"In step: {target}")
                else:
                    print(f"OUT OF STEP: {target} is {state}")
                    code = 1
        return code

    for canonical, targets in PAIRS.items():
        for target, state in sync(canonical, targets, write=True).items():
            if state == IN_STEP:
                print(f"Already in step: {target}")
            else:
                print(f"Updated {target}")
    return run_contract_test()


if __name__ == "__main__":
    sys.exit(main())
