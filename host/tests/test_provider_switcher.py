# host/tests/test_provider_switcher.py
"""The assistant switcher — source pins over the Swift.

`plans/2026-09-20-accessible-provider-switcher.md`. The rule itself —
which tiles the row draws, when it hands back to an inert word chip, what the
group tells a screen reader and where the keyboard cursor may go — is tabled
in `panel/Tests/BobPanelTests/ProviderSwitchTests.swift`. What this file
pins is what the Swift tests cannot see: that the phone's copy of
`ProviderChoice` is written exactly as the panel's, that no assistant menu
remains on either surface, that both groups carry the descriptors a screen
reader needs, and that the panel's key monitor stands aside while the row
holds the keyboard.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
RULE = PANEL / "ProviderSwitch.swift"
PHONE_RULE = PHONE / "ComposerView.swift"
CARD_SHEET = PANEL / "BoardCardSheet.swift"
ADHOC = PANEL / "AdhocTerminal.swift"
CARD_FACE = PANEL / "BoardCardView.swift"
PHONE_CARD = PHONE / "CardDetailView.swift"
MAIN = PANEL / "main.swift"
KEY_MONITOR = PANEL / "KeyMonitor.swift"
PANEL_VIEW = PANEL / "PanelView.swift"
TRIAGE = PANEL / "Triage.swift"
BOARD_STATE = PANEL / "BoardState.swift"

ENUM = "enum ProviderChoice {"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    assert start in text, f"missing block: {start!r}"
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                block = text[i : j + 1]
                assert len(block) > len(start), f"empty block: {start!r}"
                return block
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _squashed(text: str) -> str:
    return " ".join(_code(text).split())


def _line_starts(text: str, marker: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith(marker))


# --- the rule is one block, byte-equal on both surfaces ----------------------


def test_the_rule_opens_exactly_once_on_each_surface():
    assert _line_starts(_read(RULE), ENUM) == 1
    assert _line_starts(_read(PHONE_RULE), ENUM) == 1


def test_the_rule_names_every_member_the_views_read():
    block = _code(_block(_read(RULE), ENUM))
    for needle in (
        'static let groupLabel = "Assistant"',
        'static let unchosen = "not chosen"',
        'static let nobody = "Nobody yet"',
        'static let knownMarks: Set<String> = ["claude", "codex", "grok"]',
        "struct Tile: Equatable {",
        "static func tiles(tools: [String], installed: [String: Bool] = [:],",
        "static func wordChipOnly(tools: [String], selected: String) -> Bool",
        "static func value(selected: String, name: (String) -> String) -> String",
        "static func moved(cursor: Int, count: Int, step: Int) -> Int",
        "static func startCursor(tiles: [Tile]) -> Int",
    ):
        assert needle in block, needle


def test_the_rule_names_no_surface_type():
    """The `name:` closure is the seam that keeps the block portable: a
    `ProviderMark` or `Theme` inside it would compile on one side only."""
    block = _code(_block(_read(RULE), ENUM))
    for forbidden in ("ProviderMark", "PhoneProviderMark", "Theme.", "View",
                      "SwiftUI", "Color"):
        assert forbidden not in block, forbidden


def test_the_phone_copy_is_byte_equal():
    panel_block = _block(_read(RULE), ENUM)
    phone_block = _block(_read(PHONE_RULE), ENUM)
    assert panel_block == phone_block, "the phone's ProviderChoice drifted from the panel's"
    # Anti-vacuous: the block is the whole rule, not a stub.
    assert len(_squashed(panel_block)) >= 600, len(_squashed(panel_block))


def test_the_byte_pin_would_notice_a_doctored_copy():
    panel_block = _block(_read(RULE), ENUM)
    doctored = panel_block.replace("count - 1", "count")
    assert doctored != panel_block
    assert _block(doctored, ENUM) != _block(_read(PHONE_RULE), ENUM)


# --- no assistant menu remains on either surface ----------------------------


def test_the_card_window_assistant_field_holds_no_picker():
    text = _read(CARD_SHEET)
    field = _block(text, 'field("Assistant", "Who takes it when it starts.") {')
    assert "Picker(" not in field
    assert "ProviderChoice.wordChipOnly(" in field
    assert "showsNobody: true" in field
    # The model chooser beside it is untouched.
    assert 'Picker("", selection: $state.draft.model)' in text


def test_the_adhoc_sheet_keeps_only_the_project_picker():
    text = _read(ADHOC)
    assert text.count("Picker(") == 1
    assert "ProviderChoice.wordChipOnly(" in text
    assert 'Picker("", selection: $tool)' not in text


def test_the_phone_card_screen_assistant_is_no_menu():
    text = _read(PHONE_CARD)
    picker = _block(text, "private var assistantPicker: some View {")
    assert "Menu {" not in picker
    assert "PhoneProviderSwitch(" in picker
    assert "sending: pressed == .tool" in picker
    # The tile the person tapped is the selection for the whole stretch the
    # pick is on its way (until the board names the tool asked for), never
    # the Mac's old value under a dimmed row — that read as a tap that
    # missed and was tapped again.
    assert "selected: pendingTool ?? card.tool" in picker
    assert "pendingTool = name" in text
    assert "if !result.ok { pendingTool = nil }" in text
    model = _block(text, "private var modelPicker: some View {")
    assert "Text(shownModelLabel)" in model
    assert ".disabled(pressed == .model)" in model
    assert "let name = pendingModel ?? card.model" in text


def test_the_phone_composer_menu_helper_is_gone():
    text = _read(PHONE_RULE)
    assert "func picker(_ label: String" not in text
    assert 'picker("assistant"' not in text
    assert "PhoneProviderSwitch(tools: board.tools, installed: board.installed," in text


def test_the_chip_only_gate_replaced_falls_back_to_chip():
    for path in PANEL.glob("*.swift"):
        assert "fallsBackToChip" not in path.read_text(), path.name
    face = _read(CARD_FACE)
    assert "ProviderChoice.wordChipOnly(" in face
    assert "names one it no longer offers" in face


# --- what a screen reader hears ---------------------------------------------


def test_both_switchers_are_one_contained_group_named_assistant():
    for path, needle in (
        (RULE, "struct ProviderSwitch: View {"),
        (PHONE_RULE, "struct PhoneProviderSwitch: View {"),
    ):
        text = _block(_read(path), needle)
        assert text.count(".accessibilityLabel(ProviderChoice.groupLabel)") == 1, path.name
        assert text.count(".accessibilityElement(children: .contain)") == 1, path.name
        assert ".accessibilityElement(children: .ignore)" not in text, path.name
        assert "ProviderChoice.value(selected:" in text, path.name


def test_the_panel_tiles_speak_and_carry_selection():
    view = _block(_read(RULE), "struct ProviderSwitch: View {")
    assert "ProviderChoice.missingNote" in view
    assert ".accessibilityLabel(entry.installed ? Self.name(for: entry.provider)" in view
    assert ".accessibilityAddTraits(entry.selected ? [.isSelected] : [])" in view
    assert ".accessibilityLabel(ProviderChoice.nobody)" in view
    assert ".accessibilityValue(ProviderChoice.value(selected: selected," in view


def test_the_phone_tiles_hide_the_mark_and_carry_selection():
    view = _block(_read(PHONE_RULE), "struct PhoneProviderSwitch: View {")
    code = _code(view)
    mark = code.index("PhoneProviderMark(provider: tile.provider")
    # The silhouette is hidden as the very next modifier: the name beside it
    # is the fact, and a spoken mark would say the assistant twice.
    assert code[mark:].split(")", 1)[1].split()[0] == ".accessibilityHidden(true)"
    assert ".accessibilityLabel(tile.installed ? name" in view
    assert ".accessibilityAddTraits(tile.selected ? .isSelected : [])" in view
    assert "DecryptButton {" in view
    assert "AdaptiveStack(stacked: dynamicTypeSize.isAccessibilitySize" in view
    assert ".lineLimit(" not in view
    assert 'sending ? "Sending"' in view
    # A disabled plain-styled tile draws no differently, so the row dims
    # itself while a pick is on its way; the spoken value above is the
    # non-colour half of the same fact.
    assert ".opacity(sending ? 0.45 : 1)" in view


def test_the_phone_posts_no_announcement():
    for path in PHONE.glob("*.swift"):
        text = path.read_text()
        assert "UIAccessibility.post(" not in text, path.name


# --- the keyboard ------------------------------------------------------------


def test_the_panel_group_is_one_keyboard_stop():
    view = _block(_read(RULE), "struct ProviderSwitch: View {")
    assert ".focusable(interactive, interactions: .activate)" in view
    assert ".focusEffectDisabled()" in view
    for key in (".leftArrow", ".rightArrow", ".space", ".return", ".escape"):
        assert f".onKeyPress({key})" in view, key
    assert "ProviderChoice.moved(cursor: cursor, count: count, step: step)" in view
    assert ".onDisappear { onFocusChange(false) }" in view
    assert ".onChange(of: focused) { _, now in onFocusChange(now) }" in view


def test_the_monitor_stands_aside_below_the_terminal_and_above_the_verbs():
    text = _read(KEY_MONITOR)
    assert text.count("keys.controlFocused") == 1
    guard = text.index("if self.keys.controlFocused { return event }")
    terminal = text.index("if self.keys.terminalFocused || TerminalFocus.holdsCaret(")
    modified = text.index("let modified = event.modifierFlags")
    assert terminal < guard < modified


def test_the_focus_fact_rides_board_state_to_the_router():
    assert _read(TRIAGE).count("var controlFocused") == 1
    board_state = _read(BOARD_STATE)
    # An identity, never a bool: another card's tile disappearing under a
    # snapshot must not release the row that still draws focused.
    assert "@Published var switcherFocused: String? = nil" in board_state
    assert "@Published var switcherFocused = false" not in board_state
    assert "func noteSwitcherFocus(card id: String, focused: Bool)" in board_state
    assert "return current == id ? nil : current" in board_state
    panel_view = _read(PANEL_VIEW)
    assert ".onChange(of: board.switcherFocused)" in panel_view
    assert "keys.controlFocused = holder != nil" in panel_view
    assert panel_view.count("keys.controlFocused = false") >= 1
    assert "board.switcherFocused = false" not in panel_view
    card_face = _read(CARD_FACE)
    assert "state.noteSwitcherFocus(card: card.id," in card_face
    assert "state.switcherFocused = $0" not in card_face


def test_a_hide_releases_the_focus_and_not_only_the_flag():
    """`hide()`, the close path and the occlusion edge resign the first
    responder while a switcher holds the slot, so `@FocusState` really goes
    false; a cleared flag over a still-focused row would come back with the
    ring drawn and every key eaten. Guarded on the switcher, so the terminal's
    caret and a dictation-promoted field are never the responder resigned."""
    text = _read(MAIN)
    helper = _block(text, "private func releaseSwitcherFocus(reason: String) {")
    assert "guard boardState.switcherFocused != nil else { return }" in helper
    assert "panel?.makeFirstResponder(nil)" in helper
    assert 'releaseSwitcherFocus(reason: "hide")' in text
    assert 'releaseSwitcherFocus(reason: "close")' in text
    assert 'if !seen { releaseSwitcherFocus(reason: "occluded") }' in text
    hide = _block(text, "func hide(yieldToRemembered: Bool = true) {")
    # Before the window leaves: the resignation must land while the row is
    # still on a window that can deliver it.
    assert hide.index("releaseSwitcherFocus") < hide.index("panel.orderOut(nil)")


def test_the_switcher_does_not_reuse_the_editing_flag():
    """Escape on `editing` clears the board search; a focused switcher must
    not ride that flag."""
    panel_view = _read(PANEL_VIEW)
    for line in panel_view.splitlines():
        if "keys.editing =" in line:
            assert "switcherFocused" not in line, line


def test_the_press_bearing_phone_screens_are_on_the_main_actor():
    """A press's mark is `@State` written by a `Task` a *method* starts
    (`setTool`, `setModel`, `send`), and a task made inside a method that is
    not isolated runs off the main actor: the QUEUED word landed on a
    background thread and showed only on the next unrelated redraw — a return
    to the app (21 Sep 2026). The three screens that draw a press mark are on
    the main actor, so every task they start inherits it."""
    root = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"
    for name, decl in (("CardDetailView.swift", "struct PhoneCardDetailView: View {"),
                       ("AgentDetailView.swift", "struct AgentDetailView: View {"),
                       ("AnswerBox.swift", "struct AnswerBox: View {")):
        text = (root / name).read_text()
        assert f"@MainActor\n{decl}" in text, name
    # The one pure helper other screens call synchronously stays callable.
    agent = (root / "AgentDetailView.swift").read_text()
    assert "nonisolated static func elapsed(" in agent
