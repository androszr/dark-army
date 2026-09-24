#!/usr/bin/env python3
"""Copy the shunt skill from the agent-pack template onto Dark Army's own two
skill trees.

The one text is the pack template,
``host/dark_army_menubar/agent_pack/template/.claude/skills/shunt/`` —
five files (``SKILL.md``, ``bulk_read.py``, ``code_write.py``, ``exempt.py``,
``workers.json``), placeholder-free, which every enrolled project receives
through the pack. Dark Army's own checkout is never written by the pack
(``pack_install`` refuses it), so this script is how its copies keep up:
``.claude/skills/shunt/`` (Claude) and ``.agents/skills/shunt/`` (Codex and
Grok), the latter plus ``agents/openai.yaml`` composed by
``pack_render.openai_yaml`` exactly as ``mirror_skills`` composes it for a
rendered project.

``sync_review_skill.py``'s shape: idempotent (a target already in step is
not touched and its mtime does not move), atomic (a temporary sibling and
``os.replace``), bytes in and bytes out. ``--check`` writes nothing and exits
1 when any copy has drifted; ``host/tests/test_shunt_skill.py`` pins the same
byte identity.
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
sys.path.insert(0, str(ROOT / "host"))
from dark_army_menubar import pack_render  # noqa: E402  (host/ is not on the path)

TEMPLATE = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/shunt"
CLAUDE = ROOT / ".claude/skills/shunt"
AGENTS = ROOT / ".agents/skills/shunt"
FILES = ("SKILL.md", "bulk_read.py", "code_write.py", "exempt.py", "workers.json")
OPENAI_YAML = "agents/openai.yaml"

IN_STEP = "in step"
UPDATED = "updated"
MISSING = "missing"


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one ``os.replace``; the mode is
    preserved when the file exists and ``0o644`` when it does not."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def expected(template: Path) -> dict[str, bytes]:
    """``{relative path: bytes}`` for every copy the two trees must carry."""
    out: dict[str, bytes] = {}
    for name in FILES:
        out[name] = (template / name).read_bytes()
    out[OPENAI_YAML] = pack_render.openai_yaml(out["SKILL.md"], "shunt")
    return out


def targets(template: Path, claude: Path, agents: Path) -> dict[Path, bytes]:
    wanted = expected(template)
    out: dict[Path, bytes] = {}
    for name in FILES:
        out[claude / name] = wanted[name]
        out[agents / name] = wanted[name]
    out[agents / OPENAI_YAML] = wanted[OPENAI_YAML]
    return out


def sync(template: Path, claude: Path, agents: Path, *, write: bool) -> dict[Path, str]:
    """Compare every target with the template and, when asked, repair it.
    ``"missing"`` is a target that does not exist under ``--check``; with
    ``write`` it is created and reads ``"updated"``."""
    result: dict[Path, str] = {}
    for target, payload in targets(template, claude, agents).items():
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
    command = [str(pytest), "-q", "tests/test_shunt_skill.py"]
    return subprocess.run(command, cwd=ROOT / "host", check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args(argv)

    missing = [name for name in FILES if not (TEMPLATE / name).is_file()]
    if missing:
        print(f"REFUSED: the template is missing {', '.join(missing)} under {TEMPLATE}")
        return 2

    if args.check:
        code = 0
        for target, state in sync(TEMPLATE, CLAUDE, AGENTS, write=False).items():
            if state == IN_STEP:
                print(f"In step: {target}")
            else:
                print(f"OUT OF STEP: {target} is {state}")
                code = 1
        return code

    for target, state in sync(TEMPLATE, CLAUDE, AGENTS, write=True).items():
        if state == IN_STEP:
            print(f"Already in step: {target}")
        else:
            print(f"Updated {target}")
    return run_contract_test()


if __name__ == "__main__":
    sys.exit(main())
