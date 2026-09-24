"""A complete Prep card is the handoff, and the one list of its fields stays true.

Refine and Start open a fresh session that carries nothing of the chat that
filed the card, so ``docs/context-board.md`` holds one checklist of what a
ready card must hold. It is written, not enforced, which is exactly why it
can drift: this file holds it to

1. one home, and one heading, in the subject documents;
2. the store's real ``cards`` columns, row for row and in order;
3. ``board_workflow._OBJECTIVE_NONE``, the words that already mean "stub";
4. what the pure prompt builders in ``dispatch.py`` actually send; and
5. the pointers from the ship references and ``docs/agents.md``.

Precedent is ``test_docs_current.py``: Markdown read off disk, plus a store on
a temp file (``test_card_revision.py``'s seam) and the pure builders.
"""

import re
from pathlib import Path

from dark_army_daemon import board_workflow, dispatch
from dark_army_daemon.board import BoardStore

REPO_ROOT = Path(__file__).resolve().parents[2]

BOARD_DOC = REPO_ROOT / "docs" / "context-board.md"
OTHER_SUBJECT_DOCS = (
    "docs/context-host.md",
    "docs/context-panel.md",
    "docs/context-development.md",
)
READERS = (
    ".claude/skills/ship/references/plan.md",
    ".claude/skills/ship/references/implement.md",
    ".claude/skills/ship/references/scout.md",
    "docs/agents.md",
)

HEADING = "## A complete Prep card is the handoff"
FIELDS = (
    "title",
    "summary",
    "prompt",
    "tool",
    "project",
    "root",
    "workflow",
    "priority",
    "area",
    "beneficiary",
    "intended_benefit",
    "success_criterion",
    "create_token",
)

# Only the first cell, backticked, anchored: a backticked word in another
# column, the `Field` header and the `|---|` separator never match.
_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.M)


def _section(text: str) -> str:
    start = text.index(HEADING)
    end = text.find("\n## ", start + len(HEADING))
    return text[start:] if end == -1 else text[start:end]


def _table_fields(section: str) -> list:
    return _ROW.findall(section)


def _row(section: str, field: str) -> str:
    for line in section.splitlines():
        if line.startswith(f"| `{field}` |"):
            return line
    raise AssertionError(f"no table row for {field}")


def _board_section() -> str:
    return _section(BOARD_DOC.read_text(encoding="utf-8"))


def test_the_rule_has_exactly_one_home():
    assert BOARD_DOC.read_text(encoding="utf-8").count(HEADING + "\n") == 1
    for rel in OTHER_SUBJECT_DOCS:
        assert HEADING not in (REPO_ROOT / rel).read_text(encoding="utf-8"), rel


def test_the_table_names_exactly_the_complete_card_fields():
    assert _table_fields(_board_section()) == list(FIELDS)


def test_the_parser_is_not_vacuous():
    section = _board_section()
    without_area = "\n".join(
        line for line in section.splitlines()
        if not line.startswith("| `area` |"))
    assert _table_fields(without_area) != _table_fields(section)
    assert "area" not in _table_fields(without_area)

    with_bogus = section.replace(
        "| `create_token` |", "| `bogus` | x | x | no | no |\n| `create_token` |")
    assert "bogus" in _table_fields(with_bogus)
    assert "bogus" not in _table_fields(section)


def test_every_listed_field_is_a_real_card_column(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        columns = {row[1] for row in
                   store._conn.execute("PRAGMA table_info(cards)")}
    finally:
        store.close()
    missing = [name for name in FIELDS if name not in columns]
    assert not missing, missing


def test_create_token_is_marked_create_only():
    assert "create-only" in _row(_board_section(), "create_token")


def test_the_stub_words_are_the_plan_seed_words():
    section = _board_section()
    assert "_OBJECTIVE_NONE" in section
    words = [w for w in board_workflow._OBJECTIVE_NONE if w]
    assert words, "the stub list lost its words"
    for word in words:
        assert f"`{word}`" in section, word


def test_the_rule_sentences_are_there():
    section = _board_section()
    for phrase in (
        "is the whole brief",
        "never from the prior conversation",
        "An incomplete Prep card is not ready: it stays in Prep",
        "REMOTE_ACTIONS",
    ):
        assert phrase in section, phrase


def test_what_reaches_each_session_is_what_the_table_says():
    card = {
        "title": "Treat the card as the handoff",
        "summary": "Make a complete card the brief for fresh sessions",
        "prompt": "Write the checklist into the board document.",
        "tool": "claude",
        "beneficiary": "Robert and the planners",
        "intended_benefit": "Fewer half-empty cards reach Refine",
        "success_criterion": "One written checklist exists",
        "area": "conductor",
    }
    objective = ("Robert and the planners",
                 "Fewer half-empty cards reach Refine",
                 "One written checklist exists")

    # 1. Refine gets title, summary and prompt.
    refine = dispatch.refine_prompt(card)
    for key in ("title", "summary", "prompt"):
        assert card[key] in refine, key

    # 2. An unplanned Start is the prompt and nothing else: its title and
    # summary never reach the session.
    assert dispatch.start_prompt(card) == card["prompt"]

    # 3. A planned Start is the plan line; the card's words stay behind.
    planned = dispatch.start_prompt({**card, "plan_path": "/tmp/plan.md"})
    for key in ("title", "summary", "prompt"):
        assert card[key] not in planned, key

    # 4. A scout's Start carries all three.
    scout = dispatch.scout_prompt({**card, "kind": "scout"})
    for key in ("title", "summary", "prompt"):
        assert card[key] in scout, key

    # 5. The objective block carries the three objective fields.
    block = dispatch.objective_block(card)
    for value in objective:
        assert value in block, value

    # 6. The area block is Start's alone.
    area = dispatch.area_block(card, False)
    assert area
    assert area not in refine


def test_the_readers_point_at_the_rule():
    for rel in READERS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "A complete Prep card is the handoff" in text, rel
        assert "docs/context-board.md" in text, rel
