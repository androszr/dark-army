"""The keyboard of the hosted terminal pane.

Two rules, both learned the hard way on a real pane:

* **A focused terminal owns every key it is sent.** The panel's monitor is a
  triage keyboard — Space unfolds, `d` dismisses, `s` stops, `w` closes the
  terminal — and it consumed those presses before they reached the pty. So
  Space did nothing, and typing an ordinary word with a "w" in it armed and
  then confirmed **Close terminal** on the session being typed into.
* **⌘⌫ and its family must be typed at the pty.** A ⌘ key never reaches
  SwiftTerm's own handling; it becomes a standard editing selector, and the
  ones SwiftTerm has no case for are dropped in silence.
* **Pressing a modifier must not move the caret.** `DictationFocus.promote`
  walked the view tree for the first editable text view and the board under
  the detail is kept at opacity 0, which is not hidden — so Shift, pressed to
  type a capital letter, put the caret in the board's search field and every
  letter after it was triage.

The verdict table itself is `panel/Tests/BobPanelTests/TerminalKeyTests.swift`,
which is compiled and run. These are the greps over the wiring around it.

Grep tests over the Swift sources, which no CI job compiles.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = ROOT / "panel" / "Sources" / "BobPanel" / "KeyMonitor.swift"
PANE = ROOT / "panel" / "Sources" / "BobPanel" / "TerminalPane.swift"
KEYS = ROOT / "panel" / "Sources" / "BobPanel" / "TerminalKeys.swift"
DICTATION = ROOT / "panel" / "Sources" / "BobPanel" / "Dictation.swift"


@pytest.fixture(scope="module")
def monitor() -> str:
    """The body of `installKeyMonitor`."""
    src = MAIN.read_text()
    start = src.index("func installKeyMonitor()")
    return src[start:]


def test_the_terminal_guard_stands_above_every_triage_rung(monitor):
    """Not Escape alone: the guard is above the letter verbs, the arrows
    and Space, so all of them reach the pty."""
    guard = monitor.index("TerminalFocus.holdsCaret(")
    for rung in ('case " ":', 'case "w":', 'case "s":', "case 126:", "case 36, 76:"):
        assert guard < monitor.index(rung), rung
    # And it is the *only* place the flag is consulted — an Escape-only
    # copy below would be the bug this file records.
    assert monitor.count("keys.terminalFocused") == 1


def test_the_guard_asks_appkit_and_not_only_the_flag(monitor):
    """`terminalFocused` is published from a 0.15s focus poll through a
    boolean the reply strip also writes; AppKit's own answer cannot lag."""
    assert "self.keys.terminalFocused || TerminalFocus.holdsCaret(" in monitor


def test_the_guard_sits_below_the_other_windows(monitor):
    """A card, the settings window or a sheet still owns its own keys."""
    guard = monitor.index("TerminalFocus.holdsCaret(")
    for owner in ("attachedSheet", "cardWindow?", "settingsWindow?"):
        assert monitor.index(owner) < guard, owner


def test_the_line_edits_are_typed_at_the_pty(monitor):
    """⌘⌫ was doing nothing at all; the mapped ones are consumed here."""
    assert "TerminalKeys.verdict(" in monitor
    assert "TerminalFocus.send(bytes, to: terminalWindow)" in monitor


def test_the_monitor_performs_the_verdicts_and_decides_nothing():
    """One table, one caller: the old per-event `keyBytes` is gone from both
    files so a second, disagreeing table cannot grow beside it."""
    assert "keyBytes" not in MAIN.read_text()
    assert "keyBytes" not in PANE.read_text()


def test_every_verdict_is_performed(monitor):
    for arm in ("case .type(", "case .app(", "case .nobody:", "case .swiftTerm:"):
        assert arm in monitor, arm
    for action in ("view.copy(", "view.paste(", "view.selectAll("):
        assert action in monitor, action


def test_the_table_says_what_each_key_types():
    table = KEYS.read_text()
    for label, byte in (("⌘⌫", "0x15"),   # ^U
                        ("⌘⌦", "0x0b"),   # ^K
                        ("⌘←", "0x01"),   # ^A
                        ("⌘→", "0x05"),   # ^E
                        ("⌥⌫", "0x7f"),   # delete word back
                        ("⌥← ESC b", "0x62"),
                        ("⌥→ ESC f", "0x66")):
        line = next(ln for ln in table.splitlines()
                    if label in ln and "return .type(" in ln)
        assert byte in line, (label, line)
    # fn-⌫ carries no modifier and is dropped by SwiftTerm too.
    assert "[0x1b, 0x5b, 0x33, 0x7e]" in table
    # ⇧⏎ and ⌥⏎ ride ESC CR, because nothing on the pty ever answers the
    # kitty keyboard protocol's query.
    assert "[0x1b, 0x0d]" in table
    # ⌃ combinations are SwiftTerm's own and must not be intercepted.
    assert "!control" in table


def test_the_table_is_pure_and_carries_no_preference():
    """No AppKit event in the signature, and deliberately no settings key:
    `SettingsMenuModel.rows` is the settings window's only inventory and a
    preference key can never be renamed."""
    table = KEYS.read_text()
    assert "NSEvent.ModifierFlags" in table
    assert "event: NSEvent" not in table
    settings = ROOT / "panel" / "Sources" / "BobPanel" / "SettingsMenuModel.swift"
    assert "optionAsMeta" not in settings.read_text()


def test_option_composes_characters_rather_than_acting_as_meta():
    src = PANE.read_text()
    look = src[src.index("static func apply(to view: TerminalView)"):]
    assert "view.optionAsMetaKey = false" in look
    # And it is re-asserted on every attach.
    attach = src[src.index("func attach(session: String)"):]
    assert "TerminalLook.apply(to: view)" in attach.split("func shutdown")[0]


def test_a_click_claims_the_caret():
    """AppKit does not promote a plain NSView on click and SwiftTerm's own
    `mouseDown` never asks; once the caret left, nothing brought it back."""
    src = PANE.read_text()
    assert "final class PaneTerminalView: TerminalView" in src
    pane_view = src[src.index("final class PaneTerminalView"):]
    click = pane_view[pane_view.index("override func mouseDown("):]
    assert "window?.makeFirstResponder(self)" in click
    # Before super: a click forwarded as a mouse report returns early.
    assert click.index("makeFirstResponder") < click.index("super.mouseDown")
    assert "override func viewDidMoveToWindow()" in pane_view
    assert "TerminalKeys.claimsCaret(" in pane_view


def test_the_key_window_observer_is_torn_down():
    """A dismantled pane must not keep claiming a caret in a window it no
    longer draws in."""
    src = PANE.read_text()
    watch = src[src.index("func startFocusWatch()"):]
    assert "didBecomeKeyNotification" in watch
    shutdown = src[src.index("func shutdown()"):src.index("private func open()")]
    assert "removeObserver(keyWindowObserver)" in shutdown


def test_dictation_refuses_to_take_the_caret_from_a_terminal():
    src = DICTATION.read_text()
    assert src.count("mayPromote") == 2
    promote = src[src.index("static func promote(reason: String)"):]
    assert "guard mayPromote(" in promote
    # And the modifier monitor asks whether this key is the recorded one.
    monitor = src[src.index("keyMonitor = NSEvent.addLocalMonitorForEvents"):]
    assert "isShortcutModifier(" in monitor.split("focusMonitor")[0]


def test_a_long_paste_is_cut_below_the_daemon_s_refusal():
    src = PANE.read_text()
    send = src[src.index("func send(source: TerminalView, data:"):]
    assert "TerminalKeys.chunks(" in send


def test_the_bytes_go_out_the_view_s_own_delegate():
    """The route every other keystroke takes — not a second socket."""
    src = PANE.read_text()
    send = src[src.index("static func send(_ bytes:"):]
    assert "view.terminalDelegate" in send
    assert "delegate.send(source: view, data: bytes[...])" in send


def test_focus_needs_the_key_window():
    """A pane behind an open card window is still its window's first
    responder; a flag stuck true there would swallow every triage key."""
    src = PANE.read_text()
    sample = src[src.index("private func sampleFocus()"):]
    assert "isKeyWindow == true" in sample
    assert "firstResponder === view" in sample


def test_the_osc_52_clipboard_read_stays_unanswered():
    """**The trigger is pty output, not a gesture.** SwiftTerm's
    `oscClipboard` answers a bare `ESC ] 52 ; c ; ?` by base64-ing the
    delegate's answer straight back onto the agent's input line, so a `cat` of
    an untrusted file would hand over the whole system clipboard — passwords
    and tokens — with no prompt. Terminal.app and VS Code do not implement the
    read and iTerm2 requires opt-in, so answering it would be *more*
    permissive than all three of this pane's parity targets. The write half
    stays; the phone agrees."""
    src = PANE.read_text()
    read = src[src.index("func clipboardRead(source: TerminalView)"):]
    head = read[:read.index("\n")]
    assert head.endswith("-> Data? { nil }"), head
    assert "NSPasteboard" not in head
    # The write half is the other direction and is still implemented.
    copy = src[src.index("func clipboardCopy(source: TerminalView"):]
    assert "NSPasteboard.general.setData(content, forType: .string)" in copy


def test_copy_with_no_selection_does_not_wipe_the_clipboard(monitor):
    """`validateUserInterfaceItem` gates `copy:` on `selection.active`, and
    calling the method straight bypasses it — SwiftTerm's `copy(_:)` is
    unconditional and writes an empty string, so ⌘C on an unselected screen
    would clear the system clipboard."""
    copy_arm = monitor[monitor.index("case .copy:"):monitor.index("case .paste:")]
    assert "view.selection?.active == true" in copy_arm
    assert copy_arm.index("selection?.active") < copy_arm.index("view.copy(")


def test_nothing_is_swallowed_once_the_caret_has_left(monitor):
    """The branch is entered on the 0.15s poll *or* `holdsCaret`, so for up
    to 150 ms after the caret leaves the pane the flag is still true; ⌘X / ⌘Z
    must not be taken away from the field that now has it."""
    arm = monitor[monitor.index("case .nobody:"):monitor.index("case .swiftTerm:")]
    assert "TerminalFocus.terminal(in: terminalWindow) != nil" in arm
    assert "? nil : event" in arm


def test_the_modifier_monitor_matches_a_component_not_the_whole_label():
    """A recorded `⌥⌘D` is composed as glyphs plus a letter, and the letter
    arrives as a `keyDown` no promote path watches — matching the whole label
    would mean such a shortcut promoted nothing, ever."""
    src = DICTATION.read_text()
    rule = src[src.index("static func isShortcutModifier("):]
    rule = rule[:rule.index("\n    }")]
    assert "label.contains(glyph)" in rule
