"""Every Mac verb can be reached by pointer, keyboard and screen reader.

`plans/2026-09-25-usability-accessibility-pass.md`, Phases 1 and 5. Source
pins over `panel/Sources/BobPanel`, plus the two pure pieces *run* under
`swiftc` (the `test_card_action_weight.py` harness shape): the triage table
the key monitor reads, with the legend composed from it, and the inbox row's
spoken sentence. What they cannot see — a focus ring, a key reaching a real
`NSEvent` monitor, what VoiceOver says aloud — is the plan's two manual rows.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
MONITOR = PANEL / "KeyMonitor.swift"
TRIAGE = PANEL / "Triage.swift"
TRIAGE_KEYS = PANEL / "TriageKeys.swift"
VERBS = PANEL / "Verbs.swift"
INBOX = PANEL / "Inbox.swift"
INBOX_VIEW = PANEL / "InboxView.swift"
ROW_BARS = PANEL / "RowBars.swift"
PROCESS_TABLE = PANEL / "ProcessTable.swift"
PROJECT_TABS = PANEL / "ProjectTabs.swift"
PANEL_VIEW = PANEL / "PanelView.swift"
BRAND_BAR = PANEL / "SettingsSection.swift"
CARD_VIEW = PANEL / "BoardCardView.swift"

#: Every character the monitor performs by table, and the intent it sends.
KEYS = {" ": "open", "d": "dismiss", "s": "stop", "r": "retire",
        "w": "wrapUp", "/": "focusFilter", "?": "toggleKeys"}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    text = path.read_text()
    assert text.strip(), f"empty file: {path}"
    return text


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


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
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def _swiftc() -> str:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    return swiftc


def _run_swift(tmp_path: Path, name: str, sources: list[str], args: list[str]) -> str:
    path = tmp_path / f"{name}.swift"
    executable = tmp_path / name
    path.write_text("\n".join(sources))
    built = subprocess.run([_swiftc(), str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable), *args],
                         capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


@pytest.fixture(scope="module")
def triage_probe(tmp_path_factory) -> dict:
    """The table, run: the intent per probe character and the legend line."""
    folder = tmp_path_factory.mktemp("triage-keys")
    intent = _block(_read(TRIAGE), "enum TriageIntent: Equatable {")
    verbs = _block(_read(VERBS), "enum Verbs {")
    keys = _read(TRIAGE_KEYS).replace("import Foundation", "")
    harness = (
        "for c in CommandLine.arguments.dropFirst() {\n"
        "    let key = c == \"SPACE\" ? \" \" : c\n"
        "    if let i = TriageKeys.intent(for: key) { print(\"\\(c)=\\(i)\") }\n"
        "    else { print(\"\\(c)=none\") }\n"
        "}\n"
        "print(\"LEGEND=\" + TriageLegend.line)\n"
        "for row in TriageKeys.table { print(\"WORD=\" + row.word) }\n")
    out = _run_swift(folder, "TriageProbe",
                     ["import Foundation", intent, verbs, keys, harness],
                     ["SPACE", "d", "s", "r", "w", "/", "?", "x", "\t"])
    lines = out.splitlines()
    intents = dict(line.split("=", 1) for line in lines[:9])
    legend = next(line[len("LEGEND="):] for line in lines if line.startswith("LEGEND="))
    words = [line[len("WORD="):] for line in lines if line.startswith("WORD=")]
    return {"intents": intents, "legend": legend, "words": words}


# --- the key monitor reads one table ----------------------------------------


def test_the_monitor_reads_the_intent_table_not_literal_letters():
    code = _code(_read(MONITOR))
    for letter in ("d", "s", "r", "w", " ", "/"):
        assert f'case "{letter}":' not in code, letter
    assert code.count("TriageKeys.intent(") == 1
    # A character the table does not name is handed back to AppKit.
    tail = code[code.index("TriageKeys.intent("):]
    assert "return event" in tail


def test_tab_is_handed_back_and_the_rail_tabs_ride_control_tab():
    code = _code(_read(MONITOR))
    assert code.count("case 48:") == 1
    branch = code[code.index("case 48:"):]
    branch = branch[:branch.index("default: break")]
    assert ".control" in branch
    assert "return event" in branch
    assert ".nextTab" in branch and ".prevTab" in branch
    # The chord sits below the terminal guard, like every triage rung.
    assert code.index("TerminalFocus.holdsCaret(") < code.index("case 48:")
    view = _code(_read(PANEL_VIEW))
    assert "cycleRailTab(1)" in view and "cycleRailTab(-1)" in view
    assert "let all = Tab.allCases" in _block(view, "private func cycleRailTab(")


def test_every_letter_resolves_to_its_intent(triage_probe):
    """`s` is Stop and `r` Delete, by running the table the monitor reads."""
    intents = triage_probe["intents"]
    for key, intent in KEYS.items():
        assert intents["SPACE" if key == " " else key] == intent, key
    assert intents["x"] == "none"
    assert intents["\t"] == "none", "a plain Tab must reach AppKit"


def test_the_legend_names_every_key_the_monitor_performs(triage_probe):
    legend = triage_probe["legend"]
    for word in triage_probe["words"]:
        assert word in legend, word
    for glyph in ("⎵", "D", "S", "R", "W", "/", "?", "↑↓", "⏎", "⌃⇥", "⎋"):
        assert glyph in legend, glyph
    assert "stop" in triage_probe["words"] and "delete" in triage_probe["words"]


def test_the_legend_is_a_view_behind_a_keys_chip_and_the_question_mark():
    view = _code(_read(PANEL_VIEW))
    assert "@State private var keysLegendShown = false" in view
    assert "Text(TriageLegend.line)" in view
    assert "onKeys: { keysLegendShown.toggle() }" in view
    handler = view[view.index("case .toggleKeys:"):]
    assert handler.split("\n", 2)[1].strip() == "keysLegendShown.toggle()"
    bar = _code(_read(BRAND_BAR))
    chip = bar[bar.index('Button("keys", action: onKeys)'):]
    chip = chip[:chip.index("SettingsButton(")]
    assert ".clickable()" in chip
    assert '.accessibilityLabel("Keyboard shortcuts")' in chip
    assert ".help(" not in chip, "the legend is a view, not a tooltip"
    assert _code(_read(TRIAGE)).count("case toggleKeys") == 1


# --- Stop and Delete are drawn ------------------------------------------------


def test_stopbar_and_deletebar_are_drawn_over_the_same_gate():
    bars = _code(_read(ROW_BARS))
    stop = _block(bars, "struct StopBar: View {")
    delete = _block(bars, "struct DeleteBar: View {")
    assert "actions.armedStop == agent.id" in stop
    assert "actions.stop(agent, client: client)" in stop
    assert "Verbs.stop.armedLabel : Verbs.stop.label" in stop
    assert "actions.armedRetire == agent.id" in delete
    assert "actions.retire(agent, client: client)" in delete
    assert "Verbs.delete.armedLabel : Verbs.delete.label" in delete
    for block in (stop, delete):
        assert ".buttonStyle(AlarmOutline(color: Theme.alarm" in block
        assert ".clickable(" in block
        assert ".accessibilityLabel(Verbs." in block
    pane = _code(_block(_read(PROCESS_TABLE), "struct StdoutPane: View {"))
    assert pane.count("StopBar(") == 1 and pane.count("DeleteBar(") == 1
    assert "if agent.canStop" in pane
    assert "if category == .abandoned {" in pane
    assert pane.index("StopBar(") < pane.index("LowPriorityBar(")


def test_the_small_chips_and_hide_reach_past_their_drawing():
    table = _code(_read(PROCESS_TABLE))
    chip = table[table.index("private func chip("):]
    chip = chip[:chip.index("Color.clear.frame(width: Self.chipSize")]
    assert ".contentShape(Rectangle().inset(by: -6))" in chip
    hide = table[table.index("if agent.canHide {"):]
    hide = hide[:hide.index(".accessibilityHint(")]
    assert "Text(Verbs.hide.label)" in hide
    assert ".contentShape(Rectangle().inset(by: -6))" in hide


# --- the inbox row is a button ------------------------------------------------


def test_the_inbox_row_is_a_button_with_a_spokenlabel():
    view = _code(_read(INBOX_VIEW))
    assert "onTapGesture { onOpen" not in view
    assert view.count("Inbox.spokenLabel(") == 1
    entry = _block(view, "private func entry(_ item: InboxItem, now: TimeInterval) -> some View {")
    assert "Button(action: { onOpen(item) })" in entry
    assert ".buttonStyle(.plain)" in entry
    # The spoken age rides the same tick as the drawn one, never `Date()`.
    assert "FleetAge.spoken(startedAt: item.since, now: now)" in entry
    assert "sentence: Inbox.sentence(for: item, refusal: refusal(item)," in entry
    assert "Button(Verbs.dismiss.label) { dismiss(item) }" in entry
    row = _block(view, "private func row(_ item: InboxItem) -> some View {")
    assert "TimelineView(Clocks.SecondHand()) { context in" in row
    assert "entry(item, now: context.date.timeIntervalSince1970)" in row


def test_the_spokenlabel_reads_the_drawn_fields_in_order(tmp_path):
    """Run the function itself, beside only the types it reads: the file as
    a whole also needs the fleet's models (`test_phone_inbox.py` stubs them)."""
    inbox = _read(INBOX)
    types = [_block(inbox, start) for start in (
        "enum InboxKind: Int, CaseIterable, Hashable {",
        "enum InboxWireKind: Int, Hashable {",
        "enum InboxTarget: Hashable {",
        "struct InboxItem: Identifiable, Hashable {",
    )]
    spoken = _block(inbox, "static func spokenLabel(")
    main = "import Foundation\n" + "\n".join(types) + "\nenum Inbox {\n" + spoken + "\n}\n" + (
        "let dated = InboxItem(wire: .question, project: \"p\", title: \"Zosia\",\n"
        "    detail: \"May I run the tests?\", target: .session(\"s1\"), cardId: \"\",\n"
        "    sessionId: \"s1\", since: 100)\n"
        "print(Inbox.spokenLabel(for: dated, waited: \"5 minutes\"))\n"
        "let undated = InboxItem(wire: .endedWork, project: \"p\", title: \"Ship it\",\n"
        "    detail: \"\", target: .card(\"c1\"), cardId: \"c1\")\n"
        "print(Inbox.spokenLabel(for: undated, waited: \"\"))\n"
        "print(Inbox.spokenLabel(for: dated, waited: \"1 minute\",\n"
        "    sentence: \"Dark Army cannot reach that terminal.\"))\n")
    out = _run_swift(tmp_path, "SpokenProbe", [main], [])
    assert out.splitlines() == [
        "answer, Zosia, waiting 5 minutes, May I run the tests?",
        "look at, Ship it",
        "answer, Zosia, waiting 1 minute, May I run the tests?, "
        "Dark Army cannot reach that terminal.",
    ]


# --- the tile, the tab and the dim verbs --------------------------------------


def test_a_card_tile_opens_for_a_screen_reader():
    card = _code(_read(CARD_VIEW))
    body = card[card.index(".onTapGesture(count: 2) { openDetail() }"):]
    body = body[:body.index("private var ")]
    assert ".accessibilityAddTraits(.isButton)" in body
    assert '.accessibilityAction(named: "Open card") { openDetail() }' in body
    assert ".accessibilityAction { openDetail() }" in body


def test_a_flagged_project_tab_says_needs_you_and_draws_a_mark():
    tabs = _code(_read(PROJECT_TABS))
    chip = tabs[tabs.index("private func tabChip("):]
    assert '"\\(tab.title), \\(count), needs you"' in chip
    assert 'Text("!")' in chip
    assert chip.index("Circle()") < chip.index('Text("!")')


def test_dim_verbs_carry_a_focus_affordance():
    card = _read(CARD_VIEW)
    weighted = card[card.index("private func weighted<Content: View>("):]
    weighted = weighted[:weighted.index("\n    }\n") + 6]
    assert ".underline(hovered || focused" in weighted
    assert "Theme.dim" in weighted
    assert "Theme.faint" not in weighted
    assert "DimVerbFocus {" in weighted
    focus = _block(_code(card), "private struct DimVerbFocus<Content: View>: View {")
    assert "@FocusState private var focused: Bool" in focus
    assert ".focused($focused)" in focus
    assert ".onHover { hovered = $0 }" in focus
    chip = card[card.index("private func jumpChip("):]
    chip = chip[:chip.index("\n    }\n")]
    assert "jumpHover || jumpFocus == key" in chip
    assert ".focused($jumpFocus, equals: key)" in chip


# --- a focused control takes Return and Space ---------------------------------

KEYBOARD_FOCUS = PANEL / "KeyboardFocus.swift"

#: (key code, characters, a control focused) -> hands the press to it.
HANDS = (
    (36, "\r", True, "true"),     # Return presses the focused control
    (76, "\u0003", True, "true"),  # keypad Enter too
    (49, " ", True, "true"),      # Space
    (1, "s", True, "false"),      # a letter stays triage
    (125, "", True, "false"),     # so do the arrows
    (36, "\r", False, "false"),   # nothing focused: Return is triage
    (49, " ", False, "false"),    # nothing focused: Space is triage
)


def test_the_focused_control_verdict_by_running_it(tmp_path):
    intent = _block(_read(TRIAGE), "enum TriageIntent: Equatable {")
    verbs = _block(_read(VERBS), "enum Verbs {")
    keys = _read(TRIAGE_KEYS).replace("import Foundation", "")
    harness = (
        "let args = Array(CommandLine.arguments.dropFirst())\n"
        "var i = 0\n"
        "while i + 2 < args.count {\n"
        "    let chars = args[i + 1] == \"SPACE\" ? \" \" : args[i + 1]\n"
        "    print(TriageKeys.handsToControl(keyCode: UInt16(args[i])!, characters: chars,\n"
        "                                    controlFocused: args[i + 2] == \"1\"))\n"
        "    i += 3\n"
        "}\n"
        "print(TriageKeys.intent(for: \" \").map { \"\\($0)\" } ?? \"none\")\n")
    args: list[str] = []
    for code, chars, focused, _ in HANDS:
        args += [str(code), "SPACE" if chars == " " else chars, "1" if focused else "0"]
    out = _run_swift(tmp_path, "HandsProbe",
                     ["import Foundation", intent, verbs, keys, harness], args)
    lines = out.splitlines()
    assert lines[:-1] == [expected for *_, expected in HANDS]
    # Space is still Unfold when nothing is focused: the table did not move.
    assert lines[-1] == "open"


def test_the_monitor_performs_the_verdict_below_the_guards():
    code = _code(_read(MONITOR))
    assert code.count("TriageKeys.handsToControl(") == 1
    call = code.index("TriageKeys.handsToControl(")
    assert "controlFocused: FocusedControls.shared.holdsFocus" in code[call:call + 300]
    assert code.index("TerminalFocus.holdsCaret(") < call
    assert code.index("DictationFocus.isEditing(event.window") < call
    assert call < code.index("case 36, 76:") < code.index("TriageKeys.intent(")
    assert "return event" in code[call:code.index("case 126:")]


def test_one_writer_claims_focus_and_every_claim_withdraws():
    """Only `ClaimsKeyboardFocus` notes a claim; it withdraws on disappear,
    ignores covered content and presses only for the control itself."""
    panel_sources = {p.name: _code(_read(p)) for p in sorted(PANEL.glob("*.swift"))}
    writers = [name for name, code in panel_sources.items()
               if "FocusedControls.shared.note(" in code]
    assert writers == ["KeyboardFocus.swift"]
    focus = panel_sources["KeyboardFocus.swift"]
    claims = _block(focus, "struct ClaimsKeyboardFocus: ViewModifier {")
    assert claims.count("FocusedControls.shared.note(") == 2
    assert "onChange(of: focused && !suppressed, initial: true)" in claims
    assert ".onDisappear { FocusedControls.shared.note(id, focused: false) }" in claims
    assert "guard focused, !suppressed, let onPress else { return .ignored }" in claims
    assert "claimed.focusable(false)" in claims
    registry = _block(focus, "final class FocusedControls {")
    assert "var holdsFocus: Bool { !ids.isEmpty }" in registry
    assert "func clear()" in registry
    reporter = _block(focus, "struct ReportsKeyboardFocus: ViewModifier {")
    assert ".focused($focused)" in reporter
    assert ".modifier(ClaimsKeyboardFocus(focused: focused, keys: keys, onPress: onPress))" in reporter
    for path in (INBOX_VIEW, CARD_VIEW, ROW_BARS, PROJECT_TABS, PANEL_VIEW, BRAND_BAR,
                 PROCESS_TABLE):
        assert ".reportsKeyboardFocus(" in panel_sources[path.name], path.name
    card = panel_sources["BoardCardView.swift"]
    dim = _block(card, "private struct DimVerbFocus<Content: View>: View {")
    assert ".modifier(ClaimsKeyboardFocus(focused: focused))" in dim
    assert ".modifier(ClaimsKeyboardFocus(focused: jumpFocus == key))" in card
    # The entry and the tile press on Return only through the claim, which
    # acts for the focused view itself (a child's Return never bubbles up).
    for name in ("InboxView.swift", "BoardCardView.swift"):
        assert ".onKeyPress(" not in panel_sources[name], name


def test_the_remaining_drawn_verbs_report_focus():
    bars = _code(_read(ROW_BARS))
    for name in ("struct WrapUpBar: View {", "struct LowPriorityBar: View {"):
        assert ".reportsKeyboardFocus()" in _block(bars, name), name
    table = _code(_read(PROCESS_TABLE))
    chip = table[table.index("private func chip("):]
    chip = chip[:chip.index("Color.clear.frame(width: Self.chipSize")]
    assert ".reportsKeyboardFocus()" in chip
    hide = table[table.index("if agent.canHide {"):]
    hide = hide[:hide.index(".accessibilityHint(")]
    assert ".reportsKeyboardFocus()" in hide
    tabs = _code(_read(PROJECT_TABS))
    tab_chip = tabs[tabs.index("private func tabChip("):]
    assert ".disabled(!enabled)" in tab_chip[:tab_chip.index(".accessibilityLabel(")]


def test_a_focused_tile_and_inbox_entry_open_on_return():
    card = _code(_read(CARD_VIEW))
    tile = card[card.index(".onTapGesture(count: 2) { openDetail() }"):]
    tile = tile[:tile.index("private var ")]
    assert ".focusable(!keyboardSuppressed, interactions: .activate)" in tile
    assert ".reportsKeyboardFocus(keys: [.return, .space]," in tile
    # A card already being deleted takes no press.
    assert "onPress: isDeleting ? nil : { openDetail() })" in tile
    entry = _block(_code(_read(INBOX_VIEW)),
                   "private func entry(_ item: InboxItem, now: TimeInterval) -> some View {")
    assert ".reportsKeyboardFocus(onPress: { onOpen(item) })" in entry


def test_an_arrow_ends_the_focused_controls_claim(tmp_path):
    """The selection and the focus are never two cursors: the verdict, run."""
    intent = _block(_read(TRIAGE), "enum TriageIntent: Equatable {")
    verbs = _block(_read(VERBS), "enum Verbs {")
    keys = _read(TRIAGE_KEYS).replace("import Foundation", "")
    harness = ("for c in CommandLine.arguments.dropFirst() {\n"
               "    print(TriageKeys.endsControlFocus(keyCode: UInt16(c)!))\n"
               "}\n")
    codes = ["123", "124", "125", "126", "36", "76", "49", "1", "48", "53"]
    out = _run_swift(tmp_path, "EndsProbe",
                     ["import Foundation", intent, verbs, keys, harness], codes)
    assert out.split() == ["true"] * 4 + ["false"] * 6
    code = _code(_read(MONITOR))
    ends = code.index("TriageKeys.endsControlFocus(keyCode: event.keyCode)")
    assert code.index("TriageKeys.handsToControl(") < ends < code.index("case 126:")
    block = code[ends:code.index("case 126:")]
    assert "FocusedControls.shared.clear()" in block
    assert "self.panel?.makeFirstResponder(nil)" in block


def test_a_selection_change_and_a_covered_board_claim_nothing():
    view = _code(_read(PANEL_VIEW))
    watcher = view[view.index(".onChange(of: selected) { _, now in"):]
    watcher = watcher[:watcher.index("syncKeyFlags()")]
    assert "FocusedControls.shared.clear()" in watcher
    cover = view[view.index(".opacity(boardCovered ? 0 : 1)"):]
    cover = cover[:cover.index("if tab == .comm {")]
    assert ".allowsHitTesting(!boardCovered)" in cover
    assert ".environment(\\.keyboardClaimsSuppressed, boardCovered)" in cover
