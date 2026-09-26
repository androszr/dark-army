"""Evidence-only specialist workflows for cards from every project."""

from pathlib import Path

import pytest

from dark_army_daemon import board_workflow


IMPLEMENTATION = ["bc-implementer", "bc-verifier", "bc-bug-auditor"]


def _plan(root: Path, stages: str, name: str = "work.md") -> Path:
    path = root / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Work\n\n"
        f"- **Stages:** {stages}\n\n"
        "## What this does\n\nSomething useful.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("project", ["arpg-web", "dark-army", "finance-demo"])
def test_standard_implementation_fallback_covers_each_project(tmp_path, project):
    root = tmp_path / project
    root.mkdir()
    card = {
        "project": project,
        "root": str(root),
        "prompt": "Implement the accepted plan at plans/work.md",
        "workflow": "",
    }
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_explicit_workflow_is_authoritative(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    _plan(root, "from-plan | ignored")
    card = {
        "root": str(root),
        "prompt": f"Plan: {root / 'plans/work.md'}",
        "workflow": "explicit-one\nexplicit-two",
    }
    assert board_workflow.resolve(card, {root}) == ["explicit-one", "explicit-two"]


def test_plan_header_is_normalized_and_planner_is_removed_for_implementation(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = _plan(
        root,
        "bc-planner | bc-implementer | bc-verifier | bc-bug-auditor "
        "| bc-integration-reviewer",
    )
    card = {"root": str(root), "prompt": f"Plan: {plan}", "workflow": ""}
    assert board_workflow.resolve(card, {root}) == [
        "bc-implementer",
        "bc-verifier",
        "bc-bug-auditor",
        "bc-integration-reviewer",
    ]


def test_planner_is_kept_for_a_planning_brief(tmp_path):
    root = tmp_path / "finance-demo"
    root.mkdir()
    card = {"root": str(root), "prompt": "/ship design the import", "workflow": ""}
    assert board_workflow.resolve(card, {root}) == ["bc-planner"]


def test_exact_ship_implementation_command_uses_the_standard_stages(tmp_path):
    root = tmp_path / "arpg-web"
    root.mkdir()
    card = {"root": str(root), "prompt": "/ship implement plans/work.md"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_unrelated_grok_card_that_merely_mentions_ship_stays_empty(tmp_path):
    root = tmp_path / "finance-demo"
    root.mkdir()
    card = {
        "root": str(root),
        "prompt": "Run the Grok playtest. If it fails, consider using /ship later.",
    }
    assert board_workflow.resolve(card, {root}) == []


def test_an_attached_plan_path_is_implementation_even_when_the_prompt_is_the_leftover_idea(
        tmp_path):
    """`attach_plan` writes `plan_path` and leaves `prompt` as the idea.
    That card is still implementation — the plan is on the card."""
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = _plan(
        root,
        "bc-planner | bc-implementer | bc-verifier | bc-bug-auditor",
    )
    card = {
        "root": str(root),
        "prompt": ("Enable users to reorder items within the pipeline "
                   "queue through drag-and-drop or similar interaction."),
        "plan_path": str(plan),
        "workflow": "",
    }
    assert board_workflow.resolve(card, {root}) == [
        "bc-implementer",
        "bc-verifier",
        "bc-bug-auditor",
    ]


def test_stages_below_the_first_h2_are_not_metadata(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = root / "work.md"
    plan.write_text(
        "# Work\n\n## Notes\n\n- **Stages:** invented | history\n",
        encoding="utf-8",
    )
    card = {"root": str(root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_outside_root_plan_is_not_read(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    plan = _plan(outside, "outside-stage")
    card = {"root": str(root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_relative_path_traversal_plan_is_not_read(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = _plan(tmp_path, "outside-stage", name="outside.md")
    card = {"root": str(root), "prompt": "Plan: ../plans/outside.md"}
    assert plan.exists()
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_symlink_escape_plan_is_not_read(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    plan = _plan(outside, "outside-stage")
    link = root / "plans"
    link.symlink_to(plan.parent, target_is_directory=True)
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_known_project_root_can_contain_an_absolute_plan(tmp_path):
    card_root = tmp_path / "scratch"
    card_root.mkdir()
    known = tmp_path / "arpg-web"
    known.mkdir()
    plan = _plan(known, "bc-implementer | custom-check")
    card = {"root": str(card_root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {known}) == ["bc-implementer", "custom-check"]


def test_relative_plan_uses_the_card_root_not_another_projects_same_path(tmp_path):
    card_root = tmp_path / "arpg-web"
    other_root = tmp_path / "finance-demo"
    card_root.mkdir()
    other_root.mkdir()
    _plan(card_root, "bc-implementer | arpg-check")
    _plan(other_root, "bc-implementer | stock-check")
    card = {"root": str(card_root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {card_root, other_root}) == [
        "bc-implementer", "arpg-check",
    ]


def test_missing_relative_plan_never_falls_through_to_another_project(tmp_path):
    card_root = tmp_path / "arpg-web"
    other_root = tmp_path / "finance-demo"
    card_root.mkdir()
    other_root.mkdir()
    _plan(other_root, "bc-implementer | wrong-project")
    card = {"root": str(card_root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {card_root, other_root}) == IMPLEMENTATION


def test_traversal_into_other_known_root_never_reads_that_projects_plan(tmp_path):
    card_root = tmp_path / "arpg-web"
    other_root = tmp_path / "finance-demo"
    card_root.mkdir()
    other_root.mkdir()
    _plan(other_root, "bc-implementer | wrong-project")
    card = {
        "root": str(card_root),
        "prompt": "Plan: ../finance-demo/plans/work.md",
    }
    assert board_workflow.resolve(card, {card_root, other_root}) == IMPLEMENTATION


def test_oversized_plan_is_not_read(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = root / "large.md"
    plan.write_text(
        "# Work\n- **Stages:** invented\n" + "x" * board_workflow.MAX_PLAN_BYTES,
        encoding="utf-8",
    )
    card = {"root": str(root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_malformed_utf8_plan_is_not_read(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    plan = root / "broken.md"
    plan.write_bytes(b"# Work\n- **Stages:** invented\n\xff")
    card = {"root": str(root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def _stages_guidance(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "**Stages:**"
    start = text.index(marker)
    end = text.index("\n\n", start)
    return " ".join(text[start:end].replace("- **Stages:**", "**Stages:**").split())


def test_tracked_ship_templates_share_the_stage_contract_and_card_call(tmp_path):
    del tmp_path
    repo = Path(__file__).resolve().parents[2]
    codex = repo / ".agents/skills/ship/templates/plan.md"
    claude = repo / ".claude/skills/ship/templates/plan.md"
    skill = repo / ".claude/skills/ship/references/plan.md"
    assert _stages_guidance(codex) == _stages_guidance(claude)
    call = skill.read_text(encoding="utf-8")
    example = call[call.index("mcp__dark-army__dark_army_add_card({"):]
    assert "stages:" in example[:example.index("})")]


def test_unknown_home_in_a_plan_path_is_no_evidence_rather_than_an_error(tmp_path):
    """`expanduser` raises for an unknown `~user`, and this must not.

    Card text is writable by anything on the machine through `bob_add_card`,
    and the startup backfill runs inside the daemon's board `try` — a raise
    there took the whole board down for the run.
    """
    root = tmp_path / "dark-army"
    root.mkdir()
    card = {
        "root": str(root),
        "prompt": (
            "Plan: ~nobody42/plans/x.md\n\n"
            "Read that plan first, then run: /ship implement ~nobody42/plans/x.md"
        ),
        "workflow": "",
    }
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_unknown_home_in_the_card_root_is_no_evidence_rather_than_an_error(tmp_path):
    card = {"root": "~nobody42/project", "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, ["~nobody42/other"]) == IMPLEMENTATION


def test_the_shipped_template_placeholder_header_invents_nothing(tmp_path):
    """A plan still carrying the template's own guidance is not a stage list.

    Split on `[|,]` the placeholder yields six prose fragments, which used to
    persist and draw as hollow "still to come" markers — the exact false claim
    this resolver exists to avoid.
    """
    repo = Path(__file__).resolve().parents[2]
    template = (repo / ".claude/skills/ship/templates/plan.md").read_text(
        encoding="utf-8")
    root = tmp_path / "dark-army"
    (root / "plans").mkdir(parents=True)
    (root / "plans/work.md").write_text(template, encoding="utf-8")
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_a_prose_header_falls_through_to_the_command_default(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    _plan(root, "bc-implementer | whatever the plan requires here")
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


def test_a_well_formed_header_still_parses(tmp_path):
    root = tmp_path / "dark-army"
    root.mkdir()
    _plan(root, "`bc-implementer` | bc-verifier, custom.check-2")
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == [
        "bc-implementer", "bc-verifier", "custom.check-2",
    ]


def test_header_validation_never_touches_an_explicit_stage_list(tmp_path):
    """Plan step 3: explicit stages survive byte-for-byte."""
    root = tmp_path / "dark-army"
    root.mkdir()
    card = {
        "root": str(root),
        "prompt": "/ship implement plans/work.md",
        "workflow": ["a stage with spaces", "<angle>", "`ticks`"],
    }
    assert board_workflow.resolve(card, {root}) == [
        "a stage with spaces", "<angle>", "`ticks`",
    ]


def test_a_declared_reviewer_survives_the_parenthetical_the_template_invites(
        tmp_path):
    """A trailing aside must not cost the stage it is attached to.

    The template asks for `bc-integration-reviewer` "only when this plan
    requires that audit", so an author plausibly says why in brackets. Under
    all-or-nothing validation that aside dropped the whole header, and the
    command default is the same list *minus* the reviewer — a track that looks
    right and is one declared stage short.
    """
    root = tmp_path / "dark-army"
    root.mkdir()
    _plan(root, "bc-implementer | bc-verifier | bc-bug-auditor | "
                "bc-integration-reviewer (this plan touches the API surface)")
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == [
        "bc-implementer", "bc-verifier", "bc-bug-auditor",
        "bc-integration-reviewer",
    ]


def test_an_aside_does_not_rescue_a_header_that_is_really_prose(tmp_path):
    """Only a trailing bracket is cut, around a name. Prose still drops."""
    root = tmp_path / "dark-army"
    root.mkdir()
    _plan(root, "bc-implementer | whatever the plan requires (see above)")
    card = {"root": str(root), "prompt": "Plan: plans/work.md"}
    assert board_workflow.resolve(card, {root}) == IMPLEMENTATION


# --- declared_files: the panel's file table, parsed on both sides ----------

#: **One literal table, pinned at both ends.** Its twin lives in
#: `panel/Tests/BobPanelTests/PipelineContractTests.swift` and must stay
#: byte-identical: `board_workflow.parse_declared_files` and
#: `PlanStructure.parseFiles` are two implementations of one rule, and a
#: disagreement means the two sides are reading different plans. Change one,
#: and the other's test fails — which is the point.
CONTRACT_TABLE = """# A plan

- **Stages:** bc-implementer

## Files to change

| File | Change |
|---|---|
| `host/dark_army_daemon/board.py` | schema 8 |
| `panel/Sources/BobPanel/PanelView.swift` | the band |

## New files

| File | Purpose |
|---|---|
| `host/dark_army_daemon/board_queue.py` | the policy |

## Out of scope

Nothing here is a table.
"""

CONTRACT_FILES_TO_CHANGE = [
    "host/dark_army_daemon/board.py",
    "panel/Sources/BobPanel/PanelView.swift",
]


def test_the_contract_table_parses_to_the_files_both_ends_agree_on():
    """`## Files to change` is the half the panel also parses. The daemon
    additionally reads `## New files`: a plan that only creates files is still
    saying what it will touch, while the panel draws one table because the
    diagram is about what the plan will change."""
    files = board_workflow.parse_declared_files(CONTRACT_TABLE)
    assert files[:2] == CONTRACT_FILES_TO_CHANGE
    assert "host/dark_army_daemon/board_queue.py" in files
    # The header row, the separator row and the prose section contribute
    # nothing — the three ways a naive parser invents a file.
    assert "File" not in files and "---" not in files
    assert not any("Nothing here" in f for f in files)


def test_declared_files_reads_a_plan_inside_the_cards_own_root(tmp_path):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "work.md"
    plan.write_text(CONTRACT_TABLE, encoding="utf-8")
    card = {"plan_path": str(plan), "root": str(root)}
    assert board_workflow.declared_files(card)[:2] == CONTRACT_FILES_TO_CHANGE
    # Relative to the card's root works too — that is how a card written by
    # hand names its plan.
    assert board_workflow.declared_files(
        {"plan_path": "plans/work.md", "root": str(root)})[:2] \
        == CONTRACT_FILES_TO_CHANGE


def test_declared_files_is_empty_for_everything_it_cannot_read(tmp_path):
    """Empty is *unknown*, and every caller reads it as "touches everything".
    So nothing here reports why — a plan outside the root, one that is gone,
    one that is too large and one with no tables are all the same answer."""
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    outside = tmp_path / "elsewhere.md"
    outside.write_text(CONTRACT_TABLE, encoding="utf-8")
    assert board_workflow.declared_files({"plan_path": str(outside),
                                          "root": str(root)}) == []
    assert board_workflow.declared_files({"plan_path": "plans/gone.md",
                                          "root": str(root)}) == []
    assert board_workflow.declared_files({"root": str(root)}) == []

    big = root / "plans" / "big.md"
    big.write_text("x" * (board_workflow.MAX_PLAN_BYTES + 1), encoding="utf-8")
    assert board_workflow.declared_files({"plan_path": str(big),
                                          "root": str(root)}) == []

    prose = root / "plans" / "prose.md"
    prose.write_text("# Work\n\n## What this does\n\nWords.\n", encoding="utf-8")
    assert board_workflow.declared_files({"plan_path": str(prose),
                                          "root": str(root)}) == []


def test_a_table_is_capped_and_a_repeat_counted_once():
    rows = "\n".join(f"| `host/f{n}.py` | x |"
                     for n in range(board_workflow.MAX_TABLE_ROWS + 5))
    text = f"## Files to change\n\n| File | Change |\n|---|---|\n{rows}\n"
    files = board_workflow.parse_declared_files(text)
    assert len(files) <= board_workflow.MAX_TABLE_ROWS
    twice = board_workflow.parse_declared_files(
        "## Files to change\n\n| `a.py` | x |\n| `a.py` | y |\n")
    assert twice == ["a.py"]


@pytest.mark.parametrize("text,want", [
    ("- **Area:** Backbone", "backbone"), ("**Area:** pocket", "pocket"),
    ("- **Area:** bad\n- **Area:** desk", ""), ("Area: desk", ""),
])
def test_parse_header_area(text,want):
    from dark_army_daemon.board_workflow import parse_header_area
    assert parse_header_area(text) == want

def test_read_plan_area(tmp_path):
    from dark_army_daemon.board_workflow import read_plan_area
    p = tmp_path / "plan.md"; p.write_text("- **Area:** Desk\n")
    assert read_plan_area(p) == "desk"
    assert read_plan_area(tmp_path / "missing") == ""

def test_area_guidance_parity():
    root = Path(__file__).resolve().parents[2]
    paths = [root / p for p in (".claude/skills/ship/templates/plan.md", ".agents/skills/ship/templates/plan.md", "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/templates/plan.md")]
    blocks = [[line for line in p.read_text().splitlines() if line.startswith(("- **Area:**", "<!-- Area:"))] for p in paths]
    assert len(blocks[0]) == 2
    assert blocks[0] == blocks[1] == blocks[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,want", [("", "backbone"), ("desk", "desk")])
async def test_area_seed_at_attach_preserves_choice(tmp_path, monkeypatch, initial, want):
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon
    store = BoardStore(tmp_path / "board.db"); store.connect()
    d = BobDaemon(); d._board = store
    path = tmp_path / "plan.md"; path.write_text("- **Area:** Backbone\n")
    card, _ = store.create({"title": "x", "root": str(tmp_path), "area": initial})
    store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
    async def publish(): pass
    monkeypatch.setattr(d, "_publish_board", publish)
    try:
        attached, detail = await d.attach_plan_by_session("r", str(path))
        assert attached is not None, detail
        assert attached["area"] == want
        assert store.get(card["id"])["area"] == want
    finally:
        store.close()

def test_area_backfill_even_with_existing_workflow(tmp_path, monkeypatch):
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon
    store = BoardStore(tmp_path / "board.db"); store.connect()
    d = BobDaemon(); d._board = store
    path = tmp_path / "plan.md"; path.write_text("- **Area:** Pocket\n")
    card, _ = store.create({"title": "x", "root": str(tmp_path), "workflow": "bc-verifier"})
    store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
    store.attach_plan(card["id"], str(path), "r")
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(tmp_path)})
    try:
        assert d._backfill_board_workflows() == 1
        assert store.get(card["id"])["area"] == "pocket"
        assert store.get(card["id"])["workflow"] == "bc-verifier"
    finally:
        store.close()


# --- The plan's objective seeds the card's empty boxes ----------------------
#
# Why this exists: nineteen of the twenty cards on the author's board were
# filed by agents through `bob_add_card`, which names no objective field, so
# their Benefit and success section stayed blank for ever. The plan is the
# one document every card has by the time it can start, so its three header
# lines are the seed — `Area:`'s route exactly.

PLAN_OBJECTIVE = ("- **Area:** Desk\n"
                  "- **Who benefits:** Anyone reading the board\n"
                  "- **Intended benefit:** They see what a card is for.\n"
                  "- **Success criterion:** Every planned card shows an objective.\n")


@pytest.mark.parametrize("text,want", [
    (PLAN_OBJECTIVE, {"beneficiary": "Anyone reading the board",
                      "intended_benefit": "They see what a card is for.",
                      "success_criterion": "Every planned card shows an objective."}),
    ("**Who benefits:** the phone user\n", {"beneficiary": "the phone user"}),
    # The template's own placeholders and an explicit NONE seed nothing.
    ("- **Who benefits:** <who this work is for>\n- **Intended benefit:** NONE\n", {}),
    ("Who benefits: prose without bold\n", {}),
    # The first header of each kind wins.
    ("- **Who benefits:** first\n- **Who benefits:** second\n", {"beneficiary": "first"}),
    ("", {}),
])
def test_parse_header_objective(text, want):
    from dark_army_daemon.board_workflow import parse_header_objective
    assert parse_header_objective(text) == want


def test_read_plan_objective(tmp_path):
    from dark_army_daemon.board_workflow import read_plan_objective
    p = tmp_path / "plan.md"; p.write_text(PLAN_OBJECTIVE)
    assert read_plan_objective(p)["beneficiary"] == "Anyone reading the board"
    assert read_plan_objective(tmp_path / "missing") == {}


def test_from_report_guidance_parity():
    """All three plan templates carry the same From report line, and both
    planner briefs tell the planner to read a named report first."""
    root = Path(__file__).resolve().parents[2]
    paths = [root / p for p in (
        ".claude/skills/ship/templates/plan.md",
        ".agents/skills/ship/templates/plan.md",
        "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/templates/plan.md")]
    labels = ("- **From report:**", "<!-- From report:")
    blocks = [[line for line in p.read_text().splitlines()
               if line.startswith(labels)] for p in paths]
    assert len(blocks[0]) == 2
    assert blocks[0] == blocks[1] == blocks[2]
    for brief in (".claude/agents/bc-planner.md",
                  "host/dark_army_menubar/agent_pack/template/.claude/agents/{{P}}-planner.md"):
        text = (root / brief).read_text()
        assert "From report:" in text, brief


def test_objective_guidance_parity():
    """All three plan templates carry the same three objective lines, and
    both planner briefs tell the planner to fill them."""
    root = Path(__file__).resolve().parents[2]
    paths = [root / p for p in (".claude/skills/ship/templates/plan.md", ".agents/skills/ship/templates/plan.md", "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/templates/plan.md")]
    labels = ("- **Who benefits:**", "- **Intended benefit:**", "- **Success criterion:**", "<!-- Objective:")
    blocks = [[line for line in p.read_text().splitlines() if line.startswith(labels)] for p in paths]
    assert len(blocks[0]) == 4
    assert blocks[0] == blocks[1] == blocks[2]
    for brief in (".claude/agents/bc-planner.md",
                  "host/dark_army_menubar/agent_pack/template/.claude/agents/{{P}}-planner.md"):
        text = (root / brief).read_text()
        assert "`**Who benefits:**`" in text and "`**Success criterion:**`" in text, brief


@pytest.mark.asyncio
async def test_objective_seed_at_attach_fills_only_empty_boxes(tmp_path, monkeypatch):
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon
    store = BoardStore(tmp_path / "board.db"); store.connect()
    d = BobDaemon(); d._board = store
    path = tmp_path / "plan.md"; path.write_text(PLAN_OBJECTIVE)
    card, _ = store.create({"title": "x", "root": str(tmp_path),
                            "beneficiary": "the person who typed this"})
    store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
    async def publish(): pass
    monkeypatch.setattr(d, "_publish_board", publish)
    try:
        attached, detail = await d.attach_plan_by_session("r", str(path))
        assert attached is not None, detail
        assert attached["beneficiary"] == "the person who typed this"
        assert attached["intended_benefit"] == "They see what a card is for."
        assert attached["success_criterion"] == "Every planned card shows an objective."
        assert attached["outcome_revision"] == card["outcome_revision"] + 1
    finally:
        store.close()


def test_objective_backfill_on_launch(tmp_path, monkeypatch):
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon
    store = BoardStore(tmp_path / "board.db"); store.connect()
    d = BobDaemon(); d._board = store
    path = tmp_path / "plan.md"; path.write_text(PLAN_OBJECTIVE)
    card, _ = store.create({"title": "x", "root": str(tmp_path),
                            "workflow": "bc-verifier", "area": "gate"})
    store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
    store.attach_plan(card["id"], str(path), "r")
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(tmp_path)})
    try:
        assert d._backfill_board_workflows() == 1
        got = store.get(card["id"])
        assert got["area"] == "gate"
        assert got["beneficiary"] == "Anyone reading the board"
        # Idempotent: the second launch finds nothing empty.
        assert d._backfill_board_workflows() == 0
    finally:
        store.close()


# --- parse_header_card: which card a batch plan is for ------------------------


def test_parse_header_card_reads_the_first_card_header_with_the_bullet_optional():
    assert board_workflow.parse_header_card("- **Card:** abc123\n") == "abc123"
    assert board_workflow.parse_header_card("**Card:**   abc123  \n") == "abc123"
    assert board_workflow.parse_header_card("- **Card:** `abc123`\n") == "abc123"
    text = "# Plan\n\n- **Area:** desk\n- **Card:** first\n\n- **Card:** second\n"
    assert board_workflow.parse_header_card(text) == "first"


def test_parse_header_card_treats_placeholders_and_none_as_no_card():
    for value in ("<id — only when the prompt gave one; omit the line otherwise>",
                  "NONE", "n/a", "", "-"):
        assert board_workflow.parse_header_card(f"- **Card:** {value}\n") == ""
    # `Card id:` in prose, a plain `Card:` word and no header at all are no card.
    assert board_workflow.parse_header_card("Card id: abc\nCard: abc\n") == ""
    assert board_workflow.parse_header_card("") == ""


def test_read_plan_card_reads_the_file_and_fails_quietly(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# p\n\n- **Card:** deadbeef\n")
    assert board_workflow.read_plan_card(plan) == "deadbeef"
    assert board_workflow.read_plan_card(tmp_path / "missing.md") == ""


# --- the `Depends on:` header (docs/card-dependencies.md) --------------------


@pytest.mark.parametrize("line,want", [
    ("- **Depends on:** Build the foundation | Ship it",
     ["Build the foundation", "Ship it"]),
    # A title holding commas is one reference: ` | ` is the only separator.
    ("- **Depends on:** Front door and server-side progress: sign-up, email "
     "links, guests",
     ["Front door and server-side progress: sign-up, email links, guests"]),
    ("- **Depends on:** Front door: sign-up, email links | `abc123`",
     ["Front door: sign-up, email links", "abc123"]),
    ('**Depends on:** "Quoted title" | Other', ["Quoted title", "Other"]),
    ("- **Depends on:** A | A | B", ["A", "B"]),
    ("- **Depends on:** <exact titles or ids of cards in this project that "
     "must finish first, or omit the line>", []),
    ("- **Depends on:** none", []),
    ("- **Depends on:** N/A", []),
    ("- **Depends on:**", []),
])
def test_parse_header_depends_on(line, want):
    assert board_workflow.parse_header_depends_on(
        "# Plan\n\n" + line + "\n- **Area:** desk\n") == want


def test_the_depends_on_header_is_capped_and_only_the_first_counts():
    many = " | ".join(f"card {i}" for i in range(12))
    got = board_workflow.parse_header_depends_on(
        f"- **Depends on:** {many}\n- **Depends on:** later prose\n")
    assert got == [f"card {i}" for i in range(board_workflow.MAX_BLOCKERS)]
    assert board_workflow.parse_header_depends_on("Depends on: prose") == []
    assert board_workflow.parse_header_depends_on("") == []


def test_read_plan_depends_on_reads_the_file_and_fails_to_nothing(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("- **Depends on:** The foundation\n")
    assert board_workflow.read_plan_depends_on(path) == ["The foundation"]
    assert board_workflow.read_plan_depends_on(tmp_path / "missing.md") == []


def test_depends_on_guidance_parity():
    """All three plan templates carry the same `Depends on:` header and
    comment; the comment is the planner's whole instruction (no role loads
    the template's words twice, `docs/card-dependencies.md`)."""
    root = Path(__file__).resolve().parents[2]
    paths = [root / p for p in (".claude/skills/ship/templates/plan.md", ".agents/skills/ship/templates/plan.md", "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/templates/plan.md")]
    blocks = [[line for line in p.read_text().splitlines()
               if line.startswith(("- **Depends on:**", "<!-- Depends on:"))]
              for p in paths]
    assert len(blocks[0]) == 2
    assert blocks[0] == blocks[1] == blocks[2]
    # The template's own placeholder seeds nothing.
    assert board_workflow.parse_header_depends_on(blocks[0][0]) == []
    assert "/api/state/pretty" in blocks[0][1] and "never guessed" in blocks[0][1]


def _attach_harness(tmp_path, monkeypatch):
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon
    store = BoardStore(tmp_path / "board.db"); store.connect()
    d = BobDaemon(); d._board = store

    async def publish(): pass
    monkeypatch.setattr(d, "_publish_board", publish)
    return d, store


@pytest.mark.asyncio
async def test_attach_seeds_the_dependencies_a_plan_names_by_title(
        tmp_path, monkeypatch):
    d, store = _attach_harness(tmp_path, monkeypatch)
    try:
        dep, _ = store.create({"title": "Build the Foundation",
                               "root": str(tmp_path)})
        other, _ = store.create({"title": "other", "root": str(tmp_path)})
        path = tmp_path / "plan.md"
        path.write_text(f"- **Depends on:** build the foundation | {other['id']}\n")
        card, _ = store.create({"title": "x", "root": str(tmp_path)})
        store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
        attached, detail = await d.attach_plan_by_session("r", str(path))
        assert attached is not None, detail
        from dark_army_daemon.board import parse_ids
        assert parse_ids(store.get(card["id"])["blocked_by"]) == [
            dep["id"], other["id"]]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_attach_never_overwrites_a_persons_dependencies(
        tmp_path, monkeypatch):
    d, store = _attach_harness(tmp_path, monkeypatch)
    try:
        mine, _ = store.create({"title": "mine", "root": str(tmp_path)})
        store.create({"title": "the plan's", "root": str(tmp_path)})
        path = tmp_path / "plan.md"
        path.write_text("- **Depends on:** the plan's\n")
        card, _ = store.create({"title": "x", "root": str(tmp_path)})
        store.update(card["id"], {"blocked_by": mine["id"],
                                  "refine_session_id": "r",
                                  "refine_state": "live"})
        attached, detail = await d.attach_plan_by_session("r", str(path))
        assert attached is not None, detail
        assert store.get(card["id"])["blocked_by"] == mine["id"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_ambiguous_title_in_the_header_seeds_nothing_and_logs(
        tmp_path, monkeypatch, caplog):
    """A partial list would let the card start before something it was
    meant to wait for, so one title naming two cards seeds none — and the
    plan is attached all the same."""
    d, store = _attach_harness(tmp_path, monkeypatch)
    try:
        store.create({"title": "twin", "root": str(tmp_path)})
        store.create({"title": "Twin", "root": str(tmp_path)})
        store.create({"title": "single", "root": str(tmp_path)})
        path = tmp_path / "plan.md"
        path.write_text("- **Depends on:** single | twin\n")
        card, _ = store.create({"title": "x", "root": str(tmp_path)})
        store.update(card["id"], {"refine_session_id": "r", "refine_state": "live"})
        with caplog.at_level("INFO"):
            attached, detail = await d.attach_plan_by_session("r", str(path))
        assert attached is not None, detail
        assert attached["plan_path"]
        assert store.get(card["id"])["blocked_by"] == ""
        assert any("dependencies not seeded" in r.getMessage()
                   and "more than one card" in r.getMessage()
                   for r in caplog.records)
    finally:
        store.close()


def test_a_card_line_quoted_in_the_plan_body_is_not_the_header():
    """A work report's own `**Card:**` line quoted below the first section
    must not name the plan's card (25 Sep 2026)."""
    from dark_army_daemon import board_workflow as bw

    body = ("# Title\n\n- **Date:** 2026-09-25\n\n## Context\n\n"
            "**Card:** Moved it to Done.\n- **Depends on:** Other card\n")
    assert bw.parse_header_card(body) == ""
    assert bw.parse_header_depends_on(body) == []
    header = "# Title\n\n- **Card:** abc123\n- **Depends on:** One | Two\n\n## Context\n"
    assert bw.parse_header_card(header) == "abc123"
    assert bw.parse_header_depends_on(header) == ["One", "Two"]
