# host/tests/test_priority_input.py
"""The priority box says what is wrong while it is typed, on both surfaces.

`PriorityInput` is the panel's restatement of `board.normalise_priority` —
empty is fine, otherwise ASCII digits from 0 to 100 — so the sentence
beside the box and the daemon's refusal cannot disagree. The phone carries
a byte-equal copy at the foot of `CardDetailView.swift`; the rule itself is
tabled in `panel/Tests/BobPanelTests/PriorityInputTests.swift`.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel" / "PriorityInput.swift"
PHONE = ROOT / "ios" / "BobPhone" / "CardDetailView.swift"
START = "enum PriorityInput {"


def _block(text: str) -> str:
    assert START in text
    i = text.index(START)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError("unbalanced")


def test_the_phone_carries_the_panels_rule_byte_for_byte():
    assert _block(PANEL.read_text()) == _block(PHONE.read_text())


def test_both_editors_draw_the_problem_beside_the_box():
    sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    assert "PriorityInput.problem(state.draft.priority)" in sheet
    assert "PriorityInput.problem(draftPriority)" in PHONE.read_text()


def test_the_words_match_the_stores_refusal():
    from dark_army_daemon.board import PRIORITY_REFUSAL, MAX_PRIORITY
    text = PANEL.read_text()
    assert f"static let limit = {MAX_PRIORITY}" in text
    assert PRIORITY_REFUSAL.split("priority must be ", 1)[1] in text


def test_every_editor_box_is_named_for_a_screen_reader():
    """Both editors route every box through a helper that puts the visible
    label and hint on the control itself (`accessibilityLabel(label)`), so
    no box is a bare "text field" to VoiceOver."""
    sheet = (ROOT / "panel/Sources/BobPanel/BoardCardSheet.swift").read_text()
    phone = PHONE.read_text()
    for text, helper in ((sheet, "private func field<Content: View, Accessory: View>("),
                         (phone, "private func editorField<Content: View>(")):
        assert helper in text
        body = text[text.index(helper):]
        body = body[:body.index("\n    }\n")]
        assert ".accessibilityLabel(label)" in body
        assert ".accessibilityHint(hint)" in body
    # The four groups, the same words on both, each a VoiceOver heading.
    for text in (sheet, phone):
        for group in ("What it is", "Where it runs", "How important", "What to do"):
            assert f'editorGroup("{group}")' in text, group
        assert ".accessibilityAddTraits(.isHeader)" in text
