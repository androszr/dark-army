#!/usr/bin/env python3
"""Keep Dark Army's vendored copy of Cloudflare's security-audit skill honest.

The skill is vendored byte for byte, one level down, at
``host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit/upstream/``
(twenty skill files plus Cloudflare's ``LICENSE``), with
``upstream-pin.json`` beside it recording the commit and the SHA-256 of every
file. Nothing under ``upstream/`` is ever edited by hand: this tool is the only
writer, so a newer Cloudflare version comes in mechanically.

Subcommands::

    pin --check             every pinned file exists with its digest, none is stray
    pin --write <clone>     (re)write the pin from a clone already copied in
    check                   ask Cloudflare's repository for HEAD, compare with the pin
    fetch <dir>             shallow-clone the repository into an empty directory
    diff <dir>              compare a fetched clone with the pin
    apply <dir>             replace upstream/, rewrite the pin, sync Dark Army's copies

A fetched clone is untrusted data: nothing inside it is ever executed, every
read is by path and git runs with no global or system configuration (the clone's own
`.git/config` is still read) and no prompts. Stdlib only,
no ``dark_army_*`` import, so it works from a bare terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/cloudflare/security-audit-skill"
TEMPLATE_SKILL = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/security-audit"
UPSTREAM_DIR = TEMPLATE_SKILL / "upstream"
PIN = TEMPLATE_SKILL / "upstream-pin.json"
SKILL_SUBDIR = "skills/security-audit"
LICENSE_NAME = "LICENSE"
CLONE_DEPTH = 100
SYNC_TOOL = ROOT / "tools/sync_security_audit_skill.py"

_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
_HOME_PATH = re.compile(r"/Users/|/home/")


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0"})
    return env


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env=git_env(), check=False,
                          capture_output=True, text=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_pin(pin: Path = PIN) -> dict:
    return json.loads(pin.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class UnsafeTree(Exception):
    """A fetched tree holds something other than plain files."""


def clone_files(clone: Path) -> dict[str, tuple[str, bytes]]:
    """``{path under upstream/ (POSIX, relative): (path in the upstream repo,
    bytes)}`` for a clone: every regular file under ``skills/security-audit/``,
    recursively, plus the root ``LICENSE``. Read by path only. A symlink or any
    other non-regular entry raises ``UnsafeTree`` naming it."""
    out: dict[str, tuple[str, bytes]] = {}
    for folder in ("skills", SKILL_SUBDIR):
        if (clone / folder).is_symlink():
            raise UnsafeTree(f"{folder} is a symlink")
    root = clone.resolve()
    skill = clone / SKILL_SUBDIR
    if skill.is_dir():
        for item in sorted(skill.rglob("*")):
            rel = item.relative_to(skill).as_posix()
            if item.is_symlink():
                raise UnsafeTree(f"{SKILL_SUBDIR}/{rel} is a symlink")
            if item.is_dir():
                continue
            if not item.is_file():
                raise UnsafeTree(f"{SKILL_SUBDIR}/{rel} is not a regular file")
            if not item.resolve().is_relative_to(root):
                raise UnsafeTree(f"{SKILL_SUBDIR}/{rel} resolves outside the clone")
            out[rel] = (f"{SKILL_SUBDIR}/{rel}", item.read_bytes())
    licence = clone / LICENSE_NAME
    if licence.is_symlink():
        raise UnsafeTree(f"{LICENSE_NAME} is a symlink")
    if licence.exists() and not licence.is_file():
        raise UnsafeTree(f"{LICENSE_NAME} is not a regular file")
    if licence.is_file() and not licence.resolve().is_relative_to(root):
        raise UnsafeTree(f"{LICENSE_NAME} resolves outside the clone")
    if licence.is_file():
        out[LICENSE_NAME] = (LICENSE_NAME, licence.read_bytes())
    return out


def pin_problems(upstream: Path = UPSTREAM_DIR, pin: Path = PIN) -> list[str]:
    """Every way ``upstream/`` differs from the pin; empty means pristine."""
    data = read_pin(pin)
    files = data.get("files", {})
    problems: list[str] = []
    for name, row in sorted(files.items()):
        target = upstream / name
        if not target.is_file():
            problems.append(f"missing: {name}")
        elif sha256(target.read_bytes()) != row["sha256"]:
            problems.append(f"changed: {name}")
    if upstream.is_dir():
        for item in sorted(upstream.rglob("*")):
            if item.is_file():
                name = item.relative_to(upstream).as_posix()
                if name not in files:
                    problems.append(f"unpinned: {name}")
    else:
        problems.append(f"missing folder: {upstream}")
    return problems


def cmd_pin_check(upstream: Path = UPSTREAM_DIR, pin: Path = PIN) -> int:
    problems = pin_problems(upstream, pin)
    if problems:
        print("security-audit upstream: DRIFT")
        for line in problems:
            print(f"  {line}")
        return 1
    data = read_pin(pin)
    print(f"security-audit upstream: pristine ({data['commit'][:7]}, {len(data['files'])} files)")
    return 0


def cmd_pin_write(clone: Path, upstream: Path = UPSTREAM_DIR, pin: Path = PIN) -> int:
    log = _git("-C", str(clone), "log", "-1", "--format=%H%n%cI%n%s")
    if log.returncode != 0:
        print(f"git log failed: {log.stderr.strip()}")
        return 2
    commit, committed, subject = log.stdout.rstrip("\n").split("\n", 2)
    files = {}
    try:
        fresh = clone_files(clone)
    except UnsafeTree as error:
        print(f"REFUSED: {error}")
        return 1
    for name, (source, _data) in fresh.items():
        target = upstream / name
        if not target.is_file():
            print(f"missing under upstream/: {name}")
            return 1
        files[name] = {"source": source, "sha256": sha256(target.read_bytes())}
    write_json(pin, {"version": 1, "repository": REPOSITORY, "commit": commit,
                     "committed": committed, "subject": subject, "licence": "MIT",
                     "files": files})
    return 0


def cmd_check(pin: Path = PIN) -> int:
    pinned = read_pin(pin)["commit"]
    result = subprocess.run(["git", "ls-remote", REPOSITORY, "HEAD"], env=git_env(),
                            check=False, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        print(f"could not reach {REPOSITORY}: {(result.stderr or 'no answer').strip()}")
        return 2
    head = result.stdout.split()[0]
    if head == pinned:
        print(f"up to date ({pinned[:7]})")
    else:
        print(f"upstream HEAD {head} differs from the pin {pinned}")
    return 0


def cmd_fetch(directory: Path) -> int:
    if directory.exists() and any(directory.iterdir()):
        print(f"REFUSED: {directory} is not empty")
        return 2
    result = _git("clone", f"--depth={CLONE_DEPTH}", "--single-branch", "--",
                  REPOSITORY, str(directory))
    if result.returncode != 0:
        print(f"clone failed: {result.stderr.strip()}")
        return 2
    head = _git("-C", str(directory), "rev-parse", "HEAD")
    print(head.stdout.strip())
    return 0


def _category(changed: set[str]) -> list[str]:
    lines = []
    companions = sorted(n for n in changed if n.endswith(".md")
                        and n not in {"SKILL.md", "RECONNAISSANCE.md", "HUNTING.md",
                                      "VALIDATION-AND-REPORTING.md"})
    if companions:
        lines.append("companions added, removed or changed: " + ", ".join(companions))
    workflow = sorted(changed & {"SKILL.md", "RECONNAISSANCE.md", "HUNTING.md",
                                 "VALIDATION-AND-REPORTING.md"})
    if workflow:
        lines.append("workflow files changed: " + ", ".join(workflow))
    validators = sorted(n for n in changed if n.endswith(".cjs") or n == "report-schema.json")
    if validators:
        lines.append("validators or schema changed: " + ", ".join(validators))
    if LICENSE_NAME in changed:
        lines.append("licence changed")
    return lines


def compute_diff(clone: Path, pin: Path = PIN) -> tuple[dict[str, list[str]], dict]:
    data = read_pin(pin)
    pinned = data.get("files", {})
    fresh = clone_files(clone)
    added = sorted(set(fresh) - set(pinned))
    removed = sorted(set(pinned) - set(fresh))
    changed = sorted(n for n in set(fresh) & set(pinned)
                     if sha256(fresh[n][1]) != pinned[n]["sha256"])
    return {"added": added, "removed": removed, "changed": changed}, data


def cmd_diff(clone: Path, pin: Path = PIN) -> int:
    try:
        delta, data = compute_diff(clone, pin)
        fresh = clone_files(clone)
    except UnsafeTree as error:
        print(f"REFUSED: {error}")
        return 1
    head = _git("-C", str(clone), "rev-parse", "HEAD").stdout.strip()
    print(f"pin {data['commit']}")
    print(f"new {head}")
    reach = _git("-C", str(clone), "cat-file", "-e", f"{data['commit']}^{{commit}}")
    if reach.returncode == 0:
        log = _git("-C", str(clone), "log", "--oneline", f"{data['commit']}..HEAD")
        print("commits since the pin:")
        print(log.stdout.rstrip() or "  (none)")
    else:
        print(f"the pinned commit is not within the last {CLONE_DEPTH} commits")
    pinned = data.get("files", {})
    for name in delta["added"]:
        print(f"added: {name} -> {sha256(fresh[name][1])}")
    for name in delta["removed"]:
        print(f"removed: {name} (was {pinned[name]['sha256']})")
    for name in delta["changed"]:
        print(f"changed: {name} {pinned[name]['sha256']} -> {sha256(fresh[name][1])}")
    if not any(delta.values()):
        print("no file differs from the pin")
    for line in _category(set().union(*map(set, delta.values()))):
        print(line)
    return 0


def _refusals(clone: Path, fresh: dict[str, tuple[str, bytes]]) -> list[str]:
    reasons = []
    if LICENSE_NAME not in fresh:
        reasons.append(f"{clone / LICENSE_NAME} is missing")
    for name, (_source, data) in fresh.items():
        text = data.decode("utf-8", errors="replace")
        if _PLACEHOLDER.search(text):
            reasons.append(f"{name} carries a {{{{PLACEHOLDER}}}}; the pack's render would refuse it")
        if _HOME_PATH.search(text):
            reasons.append(f"{name} carries a /Users/ or /home/ path; the pack's contract test would refuse it")
    return reasons


def cmd_apply(clone: Path, upstream: Path = UPSTREAM_DIR, pin: Path = PIN,
              sync_tool: Path = SYNC_TOOL) -> int:
    origin = _git("-C", str(clone), "config", "--get", "remote.origin.url")
    if origin.returncode != 0 or origin.stdout.strip() != REPOSITORY:
        print(f"REFUSED: {clone} is not a clone of {REPOSITORY}")
        return 1
    try:
        delta, data = compute_diff(clone, pin)
        fresh = clone_files(clone)
    except UnsafeTree as error:
        print(f"REFUSED: {error}")
        return 1
    if f"{SKILL_SUBDIR}/SKILL.md" not in {source for source, _d in fresh.values()}:
        print(f"REFUSED: {clone / SKILL_SUBDIR / 'SKILL.md'} is missing")
        return 1
    if not any(delta.values()):
        print("REFUSED: nothing differs from the pin")
        return 1
    reasons = _refusals(clone, fresh)
    if reasons:
        print("REFUSED:")
        for reason in reasons:
            print(f"  {reason}")
        return 1
    for name, (_source, payload) in fresh.items():
        _atomic_write(upstream / name, payload)
    for name in delta["removed"]:
        (upstream / name).unlink(missing_ok=True)
    code = cmd_pin_write(clone, upstream, pin)
    if code != 0:
        return code
    result = subprocess.run([sys.executable, str(sync_tool)], check=False)
    print("Next, run:")
    print("  cd host && .venv/bin/pytest -q -p no:cacheprovider tests/test_security_audit_skill.py "
          "tests/test_agent_pack_render.py tests/test_agent_pack_contract.py tests/test_product_name.py")
    print("  node --test host/dark_army_menubar/agent_pack/template/.claude/skills/"
          "security-audit/upstream/validate-findings.test.cjs host/dark_army_menubar/agent_pack/"
          "template/.claude/skills/security-audit/upstream/validate-coverage-ledger.test.cjs")
    print("  python3 tools/sync_security_audit_skill.py --check")
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pin_cmd = sub.add_parser("pin")
    group = pin_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", metavar="CLONE")
    sub.add_parser("check")
    for name in ("fetch", "diff", "apply"):
        sub.add_parser(name).add_argument("directory")
    args = parser.parse_args(argv)
    if args.command == "pin":
        return cmd_pin_check() if args.check else cmd_pin_write(Path(args.write))
    if args.command == "check":
        return cmd_check()
    directory = Path(args.directory)
    return {"fetch": cmd_fetch, "diff": cmd_diff, "apply": cmd_apply}[args.command](directory)


if __name__ == "__main__":
    sys.exit(main())
