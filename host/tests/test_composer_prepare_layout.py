"""The Mac composer's Prepare button sits under the idea box.

The phone already did this (`test_phone_composer_attach_prepare.py`); the
panel's composer still drew Prepare under Assistant / Project, so the flow
the idea field's own hint describes — type the thought, press Prepare, the
fields below fill in — was invisible. Position only: the gate and the
orange note travel with the button; an existing card still has neither.

Source pins in `test_phone_theme_drift.py`'s house style: the panel's layout
is not something `swift test` can see, so the shape is pinned here.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def _fields(text: str) -> str:
    """The `fields` view body, up to the next `private` declaration."""
    start = text.index("private var fields: some View {")
    rest = text.index("\n    private ", start + 1)
    return text[start:rest]


def test_prepare_is_drawn_once_and_between_the_idea_and_title_fields():
    """Type the thought, press Prepare, the fields below fill in."""
    text = _read(PANEL)
    fields = _fields(text)
    assert fields.count("prepareRow") == 1
    assert text.count('Button("Prepare")') == 1
    idea = fields.index('field("Your idea"')
    call = fields.index("prepareRow")
    # Phase two — the title and everything under it — is behind the gate.
    title = fields.index("if phaseTwoVisible {")
    assert idea < call < title, (idea, call, title)


def test_the_prepare_notes_travel_with_the_button():
    """The orange refusal and the greyed-gate sentence sit under the press,
    not down by Instructions."""
    fields = _fields(_read(PANEL))
    idea = fields.index('field("Your idea"')
    title = fields.index("if phaseTwoVisible {")
    block = fields[idea:title]
    assert "prepareNote" in block
    assert "PrepareGate.missing(" in block
    assert "prepareNote" not in fields[title:]
    assert "PrepareGate.missing(" not in fields[title:]


def test_prepare_is_composer_only():
    """An existing card has no idea box and no Prepare; the same `isComposer`
    gate covers both."""
    fields = _fields(_read(PANEL))
    idea = fields.index('field("Your idea"')
    title = fields.index("if phaseTwoVisible {")
    block = fields[idea:title]
    assert "if isComposer {" in block
    assert "cardPrepare" not in block


# --- the objective, typed before the card exists ---


def test_the_three_objective_boxes_sit_under_instructions_composer_only():
    """Once each, after Instructions and before the composer's faces, on
    the composer's own phase-two var — and nowhere on the saved card's
    grouped editor, which edits the objective in `OutcomeEditor`."""
    text = _read(PANEL)
    start = text.index("private var composerPhaseTwo: some View {")
    fields = text[start:text.index("\n    private ", start + 1)]
    for label in ('field("Who benefits"', 'field("Intended benefit"',
                  'field("Success criterion"'):
        assert fields.count(label) == 1, label
        assert text.count(label) == 1, label
    instructions = fields.index("instructionsField")
    who = fields.index('field("Who benefits"')
    benefit = fields.index('field("Intended benefit"')
    criterion = fields.index('field("Success criterion"')
    specialists = fields.index("composerSpecialists")
    assert instructions < who < benefit < criterion < specialists
    saved_start = text.index("private var savedCardEditor: some View {")
    saved = text[saved_start:text.index("\n    private ", saved_start + 1)]
    assert "Who benefits" not in saved
    assert 'field("Expected specialists"' in saved
    # The saved-card editor keeps its own three boxes, untouched.
    editor = _read(PANEL.parent / "OutcomeEditor.swift")
    assert editor.count('TextField("Who benefits"') == 1


def test_prepare_fills_an_objective_box_only_when_it_is_empty():
    """Title and summary are overwritten; the objective is the person's own
    whenever typed, so each apply is guarded on the box being empty."""
    text = _read(PANEL)
    start = text.index("private func prepare()")
    body = text[start:text.index("\n    private ", start + 1)]
    for key in ("beneficiary", "intendedBenefit", "successCriterion"):
        assert (f"if !result.{key}.isEmpty,\n"
                f"                   state.draft.outcome.{key}.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {{\n"
                f"                    state.draft.outcome.{key} = result.{key}"
                in body), key


def test_save_sends_the_objective_and_the_client_omits_an_empty_one():
    text = _read(PANEL)
    start = text.index("private func saveComposer(")
    body = text[start:]
    for line in ("beneficiary: draft.outcome.beneficiary,",
                 "intendedBenefit: draft.outcome.intendedBenefit,",
                 "successCriterion: draft.outcome.successCriterion,"):
        assert line in body, line
    client = _read(PANEL.parent / "BoardClient.swift")
    for key, name in (("beneficiary", "beneficiary"),
                      ("intended_benefit", "intendedBenefit"),
                      ("success_criterion", "successCriterion")):
        assert f'if !{name}.isEmpty {{ body["{key}"] = {name} }}' in client


def test_the_draft_banks_and_restores_the_objective():
    drafts = _read(PANEL.parent / "Drafts.swift")
    for key in ('"beneficiary": beneficiary', '"intended_benefit": intendedBenefit',
                '"success_criterion": successCriterion'):
        assert key in drafts, key
    assert "draft.outcome.beneficiary" in drafts  # worthKeeping counts it
    state = _read(PANEL.parent / "BoardState.swift")
    assert "beneficiary: draft.outcome.beneficiary," in state
    assert "next.outcome.successCriterion = d.successCriterion" in state
