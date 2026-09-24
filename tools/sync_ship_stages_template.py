#!/usr/bin/env python3
"""Synchronise the Codex ship template's ``Stages`` contract.

The managed coding sandbox can read ``.agents`` but cannot write it. Run this
script from a normal terminal to copy the canonical guidance from the Claude
template into the Codex template, then execute the focused contract test.

The edit is idempotent and atomic. It refuses to guess if either template no
longer has the expected shape.
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
SOURCE = ROOT / ".claude/skills/ship/templates/plan.md"
TARGET = ROOT / ".agents/skills/ship/templates/plan.md"
MARKER = "**Stages:**"
SURFACES_MARKER = "**Surfaces:**"


def stages_block(text: str, path: Path) -> str:
    """Return the canonical block starting at ``**Stages:**``."""
    try:
        start = text.index(MARKER)
    except ValueError as exc:
        raise RuntimeError(f"{path} has no {MARKER} contract") from exc
    end = text.find("\n\n", start)
    if end < 0:
        raise RuntimeError(f"{path} has no blank line after {MARKER}")
    return text[start:end]


def insert_stages(source: str, target: str) -> tuple[str, bool]:
    canonical = stages_block(source, SOURCE)
    if MARKER in target:
        current = stages_block(target, TARGET)
        if current != canonical:
            raise RuntimeError(
                f"{TARGET} already has a different {MARKER} contract; "
                "review it manually"
            )
        return target, False

    lines = target.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if SURFACES_MARKER in line]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {SURFACES_MARKER} line in {TARGET}, "
            f"found {len(matches)}"
        )

    index = matches[0] + 1
    prefix = "- " if lines[matches[0]].lstrip().startswith("- ") else ""
    rendered = prefix + canonical + "\n"
    lines.insert(index, rendered)
    return "".join(lines), True


def atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(text)
            temporary = Path(handle.name)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run_contract_test() -> int:
    pytest = ROOT / "host/.venv/bin/pytest"
    if not pytest.exists():
        print(f"Updated, but skipped test: {pytest} does not exist")
        return 0
    command = [
        str(pytest),
        "-q",
        "tests/test_board_workflow.py",
        "-k",
        "tracked_ship_templates",
    ]
    return subprocess.run(command, cwd=ROOT / "host", check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="check synchronisation without writing"
    )
    args = parser.parse_args()

    source = SOURCE.read_text(encoding="utf-8")
    target = TARGET.read_text(encoding="utf-8")
    updated, changed = insert_stages(source, target)

    if args.check:
        if changed:
            print(f"OUT OF STEP: {TARGET} is missing {MARKER}")
            return 1
        print("Ship template Stages contracts are in step")
        return 0

    if changed:
        atomic_write(TARGET, updated)
        print(f"Updated {TARGET}")
    else:
        print(f"Already in step: {TARGET}")
    return run_contract_test()


if __name__ == "__main__":
    sys.exit(main())
