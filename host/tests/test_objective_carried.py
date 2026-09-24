"""The objective a person typed on the card is carried into the work.

Two halves. `dispatch.objective_block` is the pure seam the daemon appends to
every Start and Refine prompt; the rest is source pins over the ship skill,
its two mirrors, the four briefs and their agent-pack templates — the
documents that tell the planner to answer the criterion, the verifier to
report against it without gating on it, and the closing note to say how the
result measured up. Those files have no other net.
"""
from pathlib import Path

import pytest

from dark_army_daemon import dispatch

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "host" / "dark_army_menubar" / "agent_pack" / "template"

SKILLS = (
    ROOT / ".claude" / "skills" / "ship" / "SKILL.md",
    ROOT / ".agents" / "skills" / "ship" / "SKILL.md",
    TEMPLATE / ".claude" / "skills" / "ship" / "SKILL.md",
)
PLANNERS = (
    ROOT / ".claude" / "agents" / "bc-planner.md",
    TEMPLATE / ".claude" / "agents" / "{{P}}-planner.md",
)
VERIFIERS = (
    ROOT / ".claude" / "agents" / "bc-verifier.md",
    TEMPLATE / ".claude" / "agents" / "{{P}}-verifier.md",
)
IMPLEMENTERS = (
    ROOT / ".claude" / "agents" / "bc-implementer.md",
    TEMPLATE / ".claude" / "agents" / "{{P}}-implementer.md",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text(encoding="utf-8")
    if path.name == "SKILL.md":
        # A ship adapter loads its references; pin the resolved text.
        for name in ("common", "plan", "implement"):
            reference = ROOT / ".claude" / "skills" / "ship" / "references" / f"{name}.md"
            if path.is_relative_to(TEMPLATE):
                reference = path.parent / "references" / f"{name}.md"
            assert reference.is_file(), f"missing reference: {reference}"
            text += "\n" + reference.read_text(encoding="utf-8")
    return text


def _prose(path: Path) -> str:
    """The file with every run of whitespace folded to one space, so a
    sentence pinned here is pinned as a sentence and not as a line wrap."""
    return " ".join(_read(path).split())


# --- the block ---------------------------------------------------------------


def test_an_empty_card_draws_no_block():
    assert dispatch.objective_block({}) == ""
    assert dispatch.objective_block(None) == ""


def test_whitespace_only_fields_draw_no_block():
    assert dispatch.objective_block({
        "beneficiary": "  ", "intended_benefit": "", "success_criterion": "\n",
    }) == ""


def test_only_the_non_blank_lines_are_drawn_under_the_head():
    block = dispatch.objective_block({
        "beneficiary": "Ops", "intended_benefit": "",
        "success_criterion": "- no page",
    })
    assert block.startswith(dispatch.OBJECTIVE_BLOCK_HEAD)
    assert "Who benefits: Ops" in block
    assert "Intended benefit" not in block
    assert "Success criterion: - no page" in block


def test_the_head_is_its_own_paragraph():
    assert dispatch.OBJECTIVE_BLOCK_HEAD == "\n\nObjective:\n"


def test_the_date_never_rides():
    block = dispatch.objective_block({"beneficiary": "Ops",
                                      "outcome_check_on": "2026-10-01"})
    assert "2026-10-01" not in block


# --- the documents -----------------------------------------------------------


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_ship_copy_names_the_objective_and_the_criterion_row(path):
    text = _read(path)
    assert "Objective:" in text
    assert "Success criterion" in text
    # The close-note sentence rule.
    assert "second sentence says how the result meets" in _prose(path)


@pytest.mark.parametrize("path", PLANNERS, ids=lambda p: str(p.relative_to(ROOT)))
def test_both_planner_briefs_tag_a_criterion_to_the_success_criterion(path):
    text = _read(path)
    assert "(success criterion)" in text
    assert "`objective`" in text
    assert "who benefits" in text.lower()


@pytest.mark.parametrize("path", VERIFIERS, ids=lambda p: str(p.relative_to(ROOT)))
def test_both_verifier_briefs_report_the_row_and_never_gate_on_it(path):
    text = _read(path)
    assert "MET / NOT MET / CANNOT TELL" in text
    assert text.count("never changes `VERIFY VERDICT`") == 1


@pytest.mark.parametrize("path", IMPLEMENTERS,
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_both_implementer_briefs_name_the_close_note_sentence(path):
    text = _read(path)
    assert "second sentence states how the result meets" in _prose(path)
    assert "Objective:" in text


def test_the_marker_and_the_two_append_sites_exist():
    daemon_board = _read(ROOT / "host" / "dark_army_daemon" / "daemon_board.py")
    assert daemon_board.count("objective_block") == 2
    assert daemon_board.count('"objective_on_create_supported": True') == 1
