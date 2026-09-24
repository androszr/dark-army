"""The root file every session reads must stay under its ceiling.

``CLAUDE.md`` is loaded into every Claude Code session and read completely by
every ship role, so since 20 Sep 2026 it is the **compact root** — universal
rules, the architecture map and the table of subject documents — under
30,000 bytes; the detail lives in ``docs/context-*.md`` and the map that
proves nothing was lost in the move is ``docs/ship-efficiency.md``
(``tools/ship_efficiency.py inventory --check``). The limit used to be
remembered rather than enforced, and it was broken within a week of the last
trim — so this test fails the run the moment the file grows past its ceiling
again. The ceiling is never raised to fit new text.

The assertion is on UTF-8 **bytes**, not ``len(str)``: the em-dashes and
arrows in the file make the two differ by several KB, and the client's
warning is on bytes of context. Stdlib + pytest only; nothing here imports
the daemon or touches the state directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CEILINGS = (
    ("CLAUDE.md", 30_000),
)

# The documents the 6 Sep 2026 trim lifted out of CLAUDE.md, and the four
# subject documents the 20 Sep 2026 compaction moved the rest into. Deleting
# one would silently re-lose the text the size test exists to protect.
CONTRACT_DOCS = (
    "docs/transport-contract.md",
    "docs/codex-contract.md",
    "docs/phone-contract.md",
    "docs/menubar-strip-contract.md",
    "docs/panel-window-contract.md",
    "docs/session-state-contract.md",
    "docs/context-board.md",
    "docs/context-panel.md",
    "docs/context-host.md",
    "docs/context-development.md",
)


def _read(name: str) -> str:
    path = REPO_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not in this checkout")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(("name", "ceiling"), CEILINGS)
def test_file_under_ceiling(name: str, ceiling: int) -> None:
    size = len(_read(name).encode("utf-8"))
    assert size < ceiling, (
        f"{name} is {size:,} bytes, {size - ceiling:,} over its {ceiling:,}-byte "
        "ceiling. Lift "
        "a subject into its docs/context-*.md document and leave a rule and a "
        "pointer behind; never raise the number."
    )


@pytest.mark.parametrize("name", CONTRACT_DOCS)
def test_contract_doc_exists(name: str) -> None:
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} is missing; CLAUDE.md points at it"
    assert path.read_text(encoding="utf-8").startswith("# "), f"{name} has no title"


def test_the_root_names_every_subject_document():
    """The compact root's map must point at the documents that exist, and the
    root itself must still carry the universal rules rather than links alone."""
    text = _read("CLAUDE.md")
    for name in CONTRACT_DOCS[6:]:
        assert f"`{name}`" in text, f"CLAUDE.md no longer points at {name}"
    for heading in ("## Universal invariants", "## What to read", "## Architecture",
                    "## How a request becomes work"):
        assert heading in text, f"CLAUDE.md lost its {heading} section"
    assert "docs/agent-context.json" in text
