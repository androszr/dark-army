"""Every keyboard on the phone has a way down, and a raised one never hides SEND.

A reply typed under a waiting agent could not be sent (25 Sep 2026): above
the keyboard the agent sheet kept its whole lead — the still, the card, the
verbs, the tabs — and the conversation column that was left gave the reply
box half of a strip. The field showed one line, SEND scrolled out of reach,
and a prose field's return key types a new line, so nothing put the
keyboard away either.

Two rules, pinned from the source in `test_phone_field_wells.py`'s house
style; the arithmetic of the second is also run on the phone
(`AgentScreenTests`, the CI phone job):

- **A way down on every field.** Each `TextField` / `TextEditor` under
  `ios/BobPhone` sits in the shared well (which carries the hide button),
  carries `.hidesKeyboard(…)` itself, or is the board's search with its own
  DONE. The drag on a scrolling screen is the second way.
- **Typing folds the lead.** While a keyboard is up over the agent sheet the
  lead and the verbs are not drawn, and the answer box may take the column
  but one turn.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
THEME = PHONE / "Theme.swift"
DETAIL = PHONE / "AgentDetailView.swift"
CONVERSATION = PHONE / "ConversationView.swift"

_FIELD = re.compile(r"\b(TextField|TextEditor|SecureField)\(")
# How far below a field's opening line its modifiers may run. The
# composer's `field(…)` helper is the longest: the well is applied to the
# `Group` holding the input, ~30 lines on.
_REACH = 40
_WAYS_DOWN = (".fieldWell(", ".hidesKeyboard(", '"× DONE"')


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _fields():
    found = []
    for path in sorted(PHONE.rglob("*.swift")):
        lines = _read(path).splitlines()
        for i, line in enumerate(lines):
            code = line.split("//", 1)[0]
            if _FIELD.search(code):
                found.append((path, i, lines))
    return found


def test_the_phone_still_has_its_fields():
    fields = _fields()
    # composer ×2, reply, Comm, pairing ×3, board search, card ×5, key
    assert len(fields) >= 14, f"parsed too few fields: {len(fields)}"


def test_every_field_has_a_way_to_put_the_keyboard_away():
    missing = []
    for path, i, lines in _fields():
        window = "\n".join(lines[i:i + _REACH])
        if not any(way in window for way in _WAYS_DOWN):
            missing.append(f"{path.name}:{i + 1}")
    assert not missing, (
        "a field with no way to hide its keyboard: " + ", ".join(missing))


def test_the_well_carries_the_hide_button():
    text = _read(THEME)
    well = text.split("struct FieldWell: ViewModifier", 1)[1].split(
        "extension View", 1)[0]
    assert ".hidesKeyboard(when: focused)" in well, (
        "the shared well no longer offers the way down")


def test_the_hide_button_resigns_whatever_holds_the_keyboard():
    text = _read(THEME)
    button = text.split("struct KeyboardHideButton: View", 1)[1].split(
        "struct HidesKeyboard: ViewModifier", 1)[0]
    assert "resignFirstResponder" in button, (
        "clearing one focus binding misses the fields that have none")
    assert '.accessibilityLabel("Hide keyboard")' in button
    assert "keyboard.chevron.compact.down" in button
    # 20 points drawn plus 12 on each side is the 44-point touch.
    assert "Rectangle().inset(by: -KeyboardHideButton.reach)" in button
    assert "static let reach: CGFloat = 12" in button
    code = "\n".join(l.split("//", 1)[0] for l in text.splitlines())
    assert "placement: .keyboard" not in code, (
        "the keyboard toolbar repeats per field and fails in detent sheets")


def test_both_hide_modifiers_show_the_button_only_while_focused():
    text = _read(THEME)
    assert text.count("if focused { KeyboardHideButton() }") == 2
    own = text.split("struct HidesKeyboardOwnFocus: ViewModifier", 1)[1].split(
        "extension View", 1)[0]
    assert "@FocusState private var focused: Bool" in own
    assert "content.focused($focused)" in own


def test_typing_scrolls_the_answer_box_above_the_keyboard():
    """25 Sep 2026: above a keyboard the reply box was a one-line strip
    whose SEND could not be reached. The conversation is one scrolling
    page now, so nothing folds: the keyboard coming up scrolls the page
    to the answer box. The terminal cover's keyboard does not count."""
    text = _read(DETAIL)
    assert "if !keyboardUp {" not in text, "nothing folds with the keyboard now"
    assert "keyboardWillShowNotification" in text
    assert "keyboardWillHideNotification" in text
    show = text.split("keyboardWillShowNotification)) { _ in", 1)[1].split(
        "keyboardWillHideNotification", 1)[0]
    assert "guard !sheets.terminalPresented else { return }" in show, (
        "the terminal cover's keyboard must not move the sheet under it")
    assert "typing: keyboardUp)" in text, (
        "the conversation is not told the keyboard is up")
    conv = _read(CONVERSATION)
    typing = conv.split(".onChange(of: typing) { _, up in", 1)[1].split(".onChange", 1)[0]
    assert "guard up else { return }" in typing
    assert "proxy.scrollTo(Self.answerAnchor, anchor: .bottom)" in typing


def test_a_drag_down_the_scrolling_screens_puts_the_keyboard_away():
    for name in ("ConversationView.swift", "AgentDetailView.swift",
                 "CardDetailView.swift", "CommView.swift"):
        assert ".scrollDismissesKeyboard(.interactively)" in _read(PHONE / name), (
            f"{name} lost its drag-to-dismiss")
