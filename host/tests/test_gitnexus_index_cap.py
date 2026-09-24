"""No source file may grow close enough to the index's size cap to fall out of it.

The code-search index (GitNexus, ``node .gitnexus/run.cjs analyze``) walks the
checkout and **silently skips every file over its per-file size cap** — the
only sign is a "Skipped 1 large files" line on a terminal nobody reads. The
default cap is 512 KB, and ``host/dark_army_daemon/daemon.py`` passed it: for
weeks every ``context`` and ``impact`` question about a daemon symbol answered
"not found" or ``risk: UNKNOWN``, while ``CLAUDE.md`` requires that question
before every edit.

The cap now lives in the tracked ``.gitnexusrc`` as ``maxFileSize`` (KB). That
file is the one seam every runner reads: ``analyze`` loads it from the repo
root whoever starts it (Claude, Codex, Grok or a person at a terminal), a
command-line ``--max-file-size`` overrides it for one run, and
``GITNEXUS_MAX_FILE_SIZE`` is overwritten by it — so the variable is set
nowhere else.

This test reads the cap from that file and fails once any tracked source file
passes three quarters of it, long before the indexer would drop it. When it
fails, either split the file or raise ``maxFileSize`` in ``.gitnexusrc``
deliberately (never above the parser's 32 MiB ceiling, where the indexer
clamps silently); never delete this test. Stdlib + pytest only; nothing here
imports the daemon or touches the state directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RC = ".gitnexusrc"

#: The indexer's own default, named in the failure message only.
DEFAULT_CAP_KB = 512
#: Tree-sitter's buffer ceiling; the indexer clamps any larger cap to it.
PARSER_CEILING_KB = 32 * 1024
#: A file past this fraction of the cap fails the suite.
WARN_FRACTION = 0.75
#: The languages in this checkout that the indexer parses.
SOURCE_SUFFIXES = (".py", ".swift", ".ts", ".tsx", ".js", ".cjs", ".mjs", ".sh")


def _rc() -> dict:
    return json.loads((REPO_ROOT / RC).read_text(encoding="utf-8"))


def cap_kb() -> int:
    """The per-file cap ``.gitnexusrc`` declares, in KB."""
    raw = _rc().get("maxFileSize")
    assert raw is not None, (
        f"{RC} has no maxFileSize; the indexer would fall back to its "
        f"{DEFAULT_CAP_KB} KB default and skip the daemon again"
    )
    if isinstance(raw, str):
        assert raw.isdigit(), f"{RC} maxFileSize {raw!r} is not a whole number of KB"
        raw = int(raw)
    assert isinstance(raw, int) and not isinstance(raw, bool), (
        f"{RC} maxFileSize {raw!r} is not a whole number of KB")
    assert 1 <= raw <= PARSER_CEILING_KB, (
        f"{RC} maxFileSize {raw} KB is outside 1..{PARSER_CEILING_KB}; above the "
        "ceiling the indexer clamps silently and the file would lie")
    return raw


def offenders(cap_kb: int, sizes: dict[str, int],
              fraction: float = WARN_FRACTION) -> list[tuple[str, int]]:
    """Every path whose byte size passes ``fraction`` of the cap, largest first."""
    limit = fraction * cap_kb * 1024
    return sorted(((path, size) for path, size in sizes.items() if size > limit),
                  key=lambda item: (-item[1], item[0]))


def _tracked_sizes() -> dict[str, int]:
    """Byte size of every tracked source file still on disk."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, check=True).stdout
    sizes: dict[str, int] = {}
    for rel in out.decode("utf-8").split("\0"):
        if not rel.endswith(SOURCE_SUFFIXES):
            continue
        path = REPO_ROOT / rel
        if path.is_file():
            sizes[rel] = path.stat().st_size
    return sizes


def test_rc_declares_the_cap():
    assert cap_kb() >= 2048
    assert _rc().get("name") == "dark-army", f"{RC} lost the index name"


def test_no_source_file_approaches_the_cap():
    cap = cap_kb()
    found = offenders(cap, _tracked_sizes())
    lines = [f"  {path}: {size:,} bytes, {size / (cap * 1024):.0%} of the {cap} KB cap"
             for path, size in found]
    assert not found, (
        "These source files are past three quarters of the code-search index's "
        "per-file cap, and the indexer skips a file over it without a word:\n"
        + "\n".join(lines)
        + f"\nSplit the file, or raise maxFileSize in {RC} deliberately; "
        "never delete this test."
    )


def test_offenders_names_the_daemon_at_the_old_cap():
    sizes = {"host/dark_army_daemon/daemon.py": 691_524,
             "host/dark_army_daemon/api_server.py": 306_272}
    assert offenders(DEFAULT_CAP_KB, sizes) == [("host/dark_army_daemon/daemon.py", 691_524)]
