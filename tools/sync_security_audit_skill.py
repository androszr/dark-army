#!/usr/bin/env python3
"""Copy the security-audit skill from the agent-pack template onto Dark Army's
own two skill trees, and the Dark Army-only update skill onto its Codex twin.

Two masters:

* the template skill folder,
  ``host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/``
  (``SKILL.md`` the Dark Army adapter, ``upstream-pin.json``, and ``upstream/``,
  Cloudflare's files byte for byte), which every enrolled project receives
  through the pack, is copied recursively to ``.claude/skills/security-audit/``
  and ``.agents/skills/security-audit/``, the latter plus ``agents/openai.yaml``
  composed by ``pack_render.openai_yaml``;
* ``.claude/skills/security-audit-update/SKILL.md`` (Dark Army-only, never in the
  template) is copied to ``.agents/skills/security-audit-update/`` plus its
  ``agents/openai.yaml``.

Dark Army's checkout is never written by the pack (``pack_install`` refuses it),
so this script is how its copies keep up. ``tools/sync_shunt_skill.py``'s shape:
idempotent, atomic, bytes in and out. ``--check`` writes nothing and exits 1 on
drift, including a stray file under a local ``security-audit/`` tree that the
master does not carry. ``host/tests/test_security_audit_skill.py`` pins the same
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

IN_STEP = "in step"
UPDATED = "updated"
MISSING = "missing"
STRAY = "stray"


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


TEMPLATE = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit"
CLAUDE = ROOT / ".claude/skills/security-audit"
AGENTS = ROOT / ".agents/skills/security-audit"
UPDATE_CLAUDE = ROOT / ".claude/skills/security-audit-update"
UPDATE_AGENTS = ROOT / ".agents/skills/security-audit-update"
OPENAI_YAML = "agents/openai.yaml"
SKILL = "SKILL.md"


def master_files(template: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` of every file under the template skill."""
    return {p.relative_to(template).as_posix(): p.read_bytes()
            for p in sorted(template.rglob("*")) if p.is_file()}


def targets(template: Path, claude: Path, agents: Path,
            update_claude: Path | None = None,
            update_agents: Path | None = None) -> dict[Path, bytes]:
    files = master_files(template)
    out: dict[Path, bytes] = {}
    for name, payload in files.items():
        out[claude / name] = payload
        out[agents / name] = payload
    out[agents / OPENAI_YAML] = pack_render.openai_yaml(files[SKILL], "security-audit")
    if update_claude is not None and update_agents is not None:
        update = (update_claude / SKILL).read_bytes()
        out[update_agents / SKILL] = update
        out[update_agents / OPENAI_YAML] = pack_render.openai_yaml(update, "security-audit-update")
    return out


def strays(template: Path, claude: Path, agents: Path) -> list[Path]:
    """Files under a local ``security-audit/`` tree the master does not carry."""
    wanted = set(master_files(template)) | {OPENAI_YAML}
    found: list[Path] = []
    for tree in (claude, agents):
        if not tree.is_dir():
            continue
        for item in sorted(tree.rglob("*")):
            if item.is_file() and item.relative_to(tree).as_posix() not in wanted:
                found.append(item)
    return found


def sync(template: Path, claude: Path, agents: Path, *, write: bool,
         update_claude: Path | None = None,
         update_agents: Path | None = None) -> dict[Path, str]:
    """Compare every target with its master and, when asked, repair it. A
    stray is reported ``"stray"`` and removed with ``write``."""
    result: dict[Path, str] = {}
    wanted = targets(template, claude, agents, update_claude, update_agents)
    for target, payload in wanted.items():
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
    for stray in strays(template, claude, agents):
        if write:
            stray.unlink()
            result[stray] = "removed"
        else:
            result[stray] = STRAY
    return result


def run_contract_test() -> int:
    pytest = ROOT / "host/.venv/bin/pytest"
    if not pytest.exists():
        print(f"Updated, but skipped test: {pytest} does not exist")
        return 0
    command = [str(pytest), "-q", "-p", "no:cacheprovider", "tests/test_security_audit_skill.py"]
    return subprocess.run(command, cwd=ROOT / "host", check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--no-test", action="store_true", help="skip the contract test after writing")
    args = parser.parse_args(argv)

    for needed in (TEMPLATE / SKILL, TEMPLATE / "upstream-pin.json"):
        if not needed.is_file():
            print(f"REFUSED: the template is missing {needed}")
            return 2
    update = UPDATE_CLAUDE if (UPDATE_CLAUDE / SKILL).is_file() else None
    update_agents = UPDATE_AGENTS if update else None

    results = sync(TEMPLATE, CLAUDE, AGENTS, write=not args.check,
                   update_claude=update, update_agents=update_agents)
    code = 0
    for target, state in results.items():
        if args.check:
            if state == IN_STEP:
                print(f"In step: {target}")
            else:
                print(f"OUT OF STEP: {target} is {state}")
                code = 1
        else:
            print(f"Already in step: {target}" if state == IN_STEP else f"{state.capitalize()} {target}")
    if args.check:
        return code
    return 0 if args.no_test else run_contract_test()


if __name__ == "__main__":
    sys.exit(main())
