"""The composer shows the specialists as faces only, not as a typed list.

Both composers — the panel's `BoardCardSheet` in composer mode and the phone's
`ComposerView` — used to draw an "expected specialists" text box with the
`SpecialistGrid` of faces directly under it, listing the same names twice.
The box is gone from both composers; the faces sit under a short caption in
the same place. The panel's *saved-card* editor has no grid under it and
keeps the typed box, so that one `TextEditor` must live in the non-composer
branch alone.

Source pins in `test_phone_theme_drift.py`'s house style: `ios/` has no test
target and the panel's layout is not something `swift test` can see, so the
shape is pinned here, where every verification pass runs it.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone" / "ComposerView.swift"
PANEL = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
DRAFTS = ROOT / "panel" / "Sources" / "BobPanel" / "Drafts.swift"

PHONE_GRID = "AreaGrid(selected: $area)"
PANEL_GRID = "areaPicker"
PANEL_EDITOR = "TextEditor(text: $state.draft.workflow)"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def test_phone_composer_has_no_specialists_box():
    phone = _read(PHONE)
    assert 'field("expected specialists"' not in phone
    assert '"composer.specialists"' not in phone


def test_phone_grid_sits_between_notes_and_photos():
    phone = _read(PHONE)
    i_photos = phone.index("photosRow")
    i_notes = phone.index('dictation: "composer.notes")')
    i_grid = phone.index(PHONE_GRID)
    assert i_photos < i_notes < i_grid


def test_phone_grid_has_a_caption():
    phone = _read(PHONE)
    i_notes = phone.index('dictation: "composer.notes")')
    i_grid = phone.index(PHONE_GRID)
    i_caption = phone.index('Text("Area")')
    assert i_notes < i_caption < i_grid


def test_phone_grid_is_guarded_on_the_same_empty_test_as_the_grid():
    """A caption that is always drawn would be a lone heading over nothing on
    an unprepared composer."""
    phone = _read(PHONE)
    i_grid = phone.index(PHONE_GRID)
    i_caption = phone.index('Text("Area")')
    i_guard = phone.rindex("if board.areasSupported {", 0, i_caption)
    assert i_guard < i_caption < i_grid


def test_panel_typed_specialists_only_outside_composer():
    panel = _read(PANEL)
    assert panel.count(PANEL_EDITOR) == 1
    i_editor = panel.index(PANEL_EDITOR)
    i_if = panel.rindex("if isComposer {", 0, i_editor)
    branch = panel[i_if:i_editor]
    assert "} else {" in branch, "the typed editor is in the else branch"
    assert 'field("Expected specialists"' in panel
    assert panel.count('field("Expected specialists"') == 1


def test_panel_grid_between_prompt_and_observation():
    panel = _read(PANEL)
    start = panel.index("private var composerPhaseTwo: some View {")
    phase_two = panel[start:panel.index("\n    private ", start + 1)]
    i_grid = phase_two.index(PANEL_GRID)
    i_prompt = phase_two.index("instructionsField")
    i_obs = phase_two.index("specialistObservation", i_grid)
    assert i_grid < i_prompt < i_obs


def test_workflow_still_rides_the_card():
    """Saving is untouched: the list Prepare produced still travels."""
    phone = _read(PHONE)
    assert 'fields["workflow"] = workflow' in phone
    assert "workflow: workflow" in phone
    panel = _read(PANEL)
    assert '"workflow": draft.workflow' in panel
    drafts = _read(DRAFTS)
    i_keep = drafts.index("worthKeeping")
    i_end = drafts.index("\n    }", i_keep)
    assert "draft.workflow" in drafts[i_keep:i_end]
