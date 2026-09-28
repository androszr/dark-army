"""Copy a project's folder-trust decision onto its card worktrees.

Codex and Claude Code each ask "trust this folder?" the first time a session
opens in a folder they have not seen, and a card's worktree
(`<root>/.worktrees/card-<id8>/`) is always a folder they have not seen. The
person already answered that question for the project; this module copies
their answer — **never invents one** — onto each new worktree, and takes the
copy away when the worktree is removed.

- **Codex** records trust as `[projects."<path>"]` / `trust_level =
  "trusted"` in `~/.codex/config.toml`. Only when the root's table says
  `trusted` and no table for the worktree exists is one appended, carrying
  `MARKER` so `unmark` can tell its own table from one the person wrote.
- **Claude Code** records it as `projects["<path>"].hasTrustDialogAccepted:
  true` in `~/.claude.json`. Only when the root's entry says so and the
  worktree's entry lacks it is the key set, every other key kept.

`_set_codex_title_writer`'s rules (`dark_army_menubar/hooks.py`): a symlinked
file is left alone, an unparseable file is left alone, every write is atomic
(a temp sibling and `os.replace`, the original mode kept), and any `OSError`
is one log line and no write. `~/.claude.json` is Claude Code's own file and
it rewrites it whole, so a race can lose a key on either side; the worst case
is the trust screen appearing once, never a broken session.

Blocking file I/O: **executor only**. Imports nothing from the menu-bar app.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib
from pathlib import Path

from . import paths

logger = logging.getLogger("dark-army.trust-marks")

#: Module globals a test repoints at a fake home. Derived from
#: `paths._home()`, so under pytest they already name a throwaway folder and
#: a test that forgets to repoint them can never write the person's own
#: configs — `paths.py`'s default-isolation rule.
CODEX_CONFIG_PATH = paths._home() / ".codex" / "config.toml"
CLAUDE_CONFIG_PATH = paths._home() / ".claude.json"

#: The comment that says a Codex table is Dark Army's copy, not the person's.
MARKER = "# managed by Dark Army"
#: Claude Code's key for an accepted trust screen.
CLAUDE_TRUST_KEY = "hasTrustDialogAccepted"


def _keys(path: str) -> list:
    """The spellings a config may key a folder under: as given, then
    realpath'd, deduplicated in that order."""
    out = []
    for key in (str(path or ""), os.path.realpath(str(path or ""))):
        key = key.rstrip(os.sep) or key
        if key and key not in out:
            out.append(key)
    return out


def _toml_key(path: str) -> str:
    """A path as a TOML basic-string key."""
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _codex_block(worktree: str) -> str:
    return (f"\n[projects.{_toml_key(worktree)}]\n"
            f'trust_level = "trusted" {MARKER}\n')


def _read(path: Path):
    """`(ok, text)`. Not ok for a symlink or an unreadable file; `""` for a
    file that is not there."""
    try:
        if path.is_symlink():
            logger.info("%s is a symlink; leaving it alone", path)
            return False, ""
        if not path.exists():
            return True, ""
        return True, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.info("could not read %s", path, exc_info=True)
        return False, ""


def _write_atomic(path: Path, text: str) -> bool:
    """Replace `path` with `text` through a temp sibling, keeping its mode."""
    try:
        mode = None
        if path.exists():
            mode = path.stat().st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                   dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, mode if mode is not None else 0o600)
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        logger.info("could not write %s", path, exc_info=True)
        return False


# --- Codex ---------------------------------------------------------------------

def _codex_load():
    """`(projects, text)` from the Codex config, or `(None, "")`."""
    ok, text = _read(CODEX_CONFIG_PATH)
    if not ok or not text.strip():
        return None, ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        logger.info("Codex config is not parseable; not reading trust")
        return None, ""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None, text
    return projects, text


def _codex_root_trusted(projects, root: str) -> bool:
    return isinstance(projects, dict) and any(
        isinstance(projects.get(k), dict)
        and projects[k].get("trust_level") == "trusted"
        for k in _keys(root))


def _codex_mark(root: str, worktree: str) -> bool:
    path = CODEX_CONFIG_PATH
    projects, text = _codex_load()
    if not _codex_root_trusted(projects, root):
        return False
    if any(k in projects for k in _keys(worktree)):
        return False
    new = text + _codex_block(worktree)
    if not text.endswith("\n"):
        new = text + "\n" + _codex_block(worktree)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        logger.info("the Codex trust copy would not parse; not writing it")
        return False
    return _write_atomic(path, new)


def _codex_unmark(worktree: str) -> bool:
    path = CODEX_CONFIG_PATH
    ok, text = _read(path)
    if not ok or not text:
        return False
    for key in _keys(worktree):
        block = _codex_block(key)
        if block in text:
            new = text.replace(block, "", 1)
        elif text.startswith(block.lstrip("\n")):
            new = text[len(block.lstrip("\n")):]
        else:
            continue
        # Re-parsed before the write: a removal that left the file
        # unreadable to Codex would be worse than the stray table.
        try:
            tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            logger.info("removing the Codex trust copy would break the "
                        "file; leaving it alone")
            return False
        return _write_atomic(path, new)
    return False


# --- Claude Code -----------------------------------------------------------------

def _claude_load():
    ok, text = _read(CLAUDE_CONFIG_PATH)
    if not ok or not text.strip():
        return None, ""
    try:
        data = json.loads(text)
    except ValueError:
        logger.info("~/.claude.json is not parseable; not copying trust")
        return None, ""
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return None, ""
    return data, text


def _claude_dump(data: dict, original: str) -> str:
    out = json.dumps(data, indent=2, ensure_ascii=False)
    return out + "\n" if original.endswith("\n") else out


def _claude_root_trusted(data, root: str) -> bool:
    projects = (data or {}).get("projects") if isinstance(data, dict) else None
    return isinstance(projects, dict) and any(
        isinstance(projects.get(k), dict)
        and projects[k].get(CLAUDE_TRUST_KEY) is True
        for k in _keys(root))


def _claude_mark(root: str, worktree: str) -> bool:
    data, text = _claude_load()
    if data is None or not _claude_root_trusted(data, root):
        return False
    projects = data["projects"]
    key = _keys(worktree)[-1]
    entry = projects.get(key)
    if entry is not None and not isinstance(entry, dict):
        return False
    if isinstance(entry, dict) and entry.get(CLAUDE_TRUST_KEY) is True:
        return False
    entry = dict(entry or {})
    entry[CLAUDE_TRUST_KEY] = True
    projects[key] = entry
    return _write_atomic(CLAUDE_CONFIG_PATH, _claude_dump(data, text))


def _claude_unmark(worktree: str) -> bool:
    data, text = _claude_load()
    if data is None:
        return False
    projects = data["projects"]
    changed = False
    for key in _keys(worktree):
        entry = projects.get(key)
        # Only an entry whose one key is ours: once Claude Code has written
        # its own keys there, the entry is Claude Code's and stays.
        if isinstance(entry, dict) and entry == {CLAUDE_TRUST_KEY: True}:
            del projects[key]
            changed = True
    if not changed:
        return False
    return _write_atomic(CLAUDE_CONFIG_PATH, _claude_dump(data, text))


# --- the verbs -----------------------------------------------------------------------

def root_trusted(root: str, tool: str) -> bool:
    """Whether the person has already trusted `root` in the assistant being
    started — Claude Code's `hasTrustDialogAccepted`, Codex's `trust_level =
    "trusted"`. The gate on the per-project setup script
    (`docs/card-worktrees.md`): a folder nobody has trusted in that tool is
    a folder whose files Dark Army will not run for them. Any other tool has
    no record Dark Army can read, so it is never trusted here. Never raises."""
    try:
        if tool == "claude":
            data, _text = _claude_load()
            return _claude_root_trusted(data, root)
        if tool == "codex":
            projects, _text = _codex_load()
            return _codex_root_trusted(projects, root)
    except Exception:
        logger.info("could not read %s's trust for %s", tool, root,
                    exc_info=True)
    return False


def mark(root: str, worktree: str) -> dict:
    """Copy the root's trust onto `worktree` in both configs, where the root
    has it. `{"codex": bool, "claude": bool}` — what was written. Never
    raises."""
    out = {"codex": False, "claude": False}
    if not root or not worktree:
        return out
    try:
        out["codex"] = _codex_mark(root, worktree)
    except Exception:
        logger.info("could not copy Codex trust", exc_info=True)
    try:
        out["claude"] = _claude_mark(root, worktree)
    except Exception:
        logger.info("could not copy Claude trust", exc_info=True)
    return out


def unmark(worktree: str) -> dict:
    """Take back what `mark` wrote for `worktree`, and nothing else. Never
    raises."""
    out = {"codex": False, "claude": False}
    if not worktree:
        return out
    try:
        out["codex"] = _codex_unmark(worktree)
    except Exception:
        logger.info("could not remove the Codex trust copy", exc_info=True)
    try:
        out["claude"] = _claude_unmark(worktree)
    except Exception:
        logger.info("could not remove the Claude trust copy", exc_info=True)
    return out
