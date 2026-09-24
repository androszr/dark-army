# host/tests/test_detail_tabs.py
"""The agent detail's two tabs — source pins over the Swift.

`plans/2026-09-06-detail-tabs-terminal-and-summary.md`. The rules themselves
are tabled in `panel/Tests/BobPanelTests/DetailTabTests.swift`; what this
file pins is what the Swift tests cannot see: that both surfaces draw the tab
bar and read `DetailTab` rather than `ownTerminal` a second time, that the
phone's copy of the enum is written exactly as the panel's, that the tab bar
is its own file-scope struct on the phone, that Details is the default on
both, that the stream and the watch follow the tab, and that a row running in
an editor kept exactly today's layout.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
DETAIL = PANEL / "AgentDetailPane.swift"
LAYOUT = PANEL / "RailLayout.swift"
TAB = PANEL / "DetailTab.swift"
PHONE_DETAIL = ROOT / "ios" / "BobPhone" / "AgentDetailView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "DetailTabTests.swift"

ENUM = "enum DetailTab: String, CaseIterable {"


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


# --- the rules live in one place, spelled the same on both sides -------------


def test_the_rules_are_pure_and_live_in_their_own_file():
    """Their own file, not `RailLayout.swift`: that file's own pin tables
    every `static func` it declares against `RailLayoutTests`, and these are
    tabled in `DetailTabTests` instead."""
    src = _read(TAB)
    block = _code(_block(src, ENUM))
    for needle in ("case details = \"Details\"",
                   "case terminal = \"Terminal\"",
                   "static let defaultTab: DetailTab = .details",
                   "static func showsTabBar(hosted: Bool) -> Bool",
                   "static func pane(hosted: Bool, tab: DetailTab) -> DetailTab",
                   "static func terminalAttached(hosted: Bool, tab: DetailTab) -> Bool"):
        assert needle in block, needle
    # Rules, not views: this file has never imported SwiftUI and must not
    # start now, and nothing here spins.
    assert "import SwiftUI" not in _code(src)
    assert "ProgressView" not in src
    assert "DetailTab" not in _read(LAYOUT)


def test_the_phone_copy_is_written_exactly_as_the_panels():
    panel = _squashed(_block(_read(TAB), ENUM))
    phone = _squashed(_block(_read(PHONE_DETAIL), ENUM))
    assert panel == phone


def test_the_rules_are_tabled_in_swift():
    src = _read(SWIFT_TESTS)
    for needle in ("DetailTab.pane(hosted: false, tab: .terminal)",
                   "DetailTab.terminalAttached(hosted: false, tab: .terminal)",
                   "DetailTab.allCases.count, 2",
                   "DetailTab.defaultTab, .details"):
        assert needle in src, needle


# --- the panel ---------------------------------------------------------------


def test_the_panel_draws_the_tab_bar_and_reads_the_rules():
    src = _read(DETAIL)
    pane = _code(_block(src, "struct AgentDetailPane"))
    assert "DetailTab.showsTabBar(hosted: agent.ownTerminal)" in pane
    assert pane.count("DetailTab.terminalAttached(") == 1
    assert "DetailTabBar(tab: $tab)" in pane
    assert "@State private var tab: DetailTab = DetailTab.defaultTab" in pane
    # The tab bar is between the hero's hairline and the body, never above
    # the header: the `card ⌗` / `jump ↗` chips stay reachable from both.
    assert pane.index("AgentDetailHeader(") < pane.index("DetailTabBar(")
    assert pane.index("DetailTabBar(") < pane.index("DetailTab.terminalAttached(")
    bar = _code(_block(src, "struct DetailTabBar: View"))
    assert "@Binding var tab: DetailTab" in bar
    assert "ForEach(DetailTab.allCases" in bar
    # Form as well as colour: the selected cell is filled.
    assert "background(tab == item ? Theme.well : Color.clear)" in bar
    assert ".clickable()" in bar
    assert "accessibilityAddTraits(tab == item ? [.isButton, .isSelected] : .isButton)" in bar
    assert ".lineLimit(" not in bar


def test_the_panel_counted_literals_did_not_move():
    src = _read(DETAIL)
    assert src.count("StdoutPane(") == 1
    assert src.count("TerminalPane(") == 1
    assert _code(src).count("GeometryReader") == 1
    assert "messageHidden: true" in src
    assert "messageHidden: false" in src
    assert src.count(".lineLimit(") == 0


def test_a_fresh_row_lands_on_details_on_the_panel():
    pane = _code(_block(_read(DETAIL), "struct AgentDetailPane"))
    change = _block(pane, ".onChange(of: agent.sessionId)")
    assert "tab = DetailTab.defaultTab" in change
    # No memory of any kind: not per session, not across launches.
    assert "@AppStorage" not in pane
    assert "UserDefaults" not in pane


def test_a_row_in_an_editor_kept_todays_layout_on_the_panel():
    pane = _code(_block(_read(DETAIL), "struct AgentDetailPane"))
    assert "ElsewhereTerminalBand(agent: agent, category: category," in pane
    assert "if !agent.ownTerminal {" in pane
    # The band belongs to the non-hosted row alone — it must sit inside that
    # guard, and the guard inside the detached branch.
    assert pane.index("if !agent.ownTerminal {") < pane.index("ElsewhereTerminalBand(")
    assert pane.index("DetailTab.terminalAttached(") < pane.index("if !agent.ownTerminal {")


def test_escape_and_the_key_router_gained_no_rung():
    main = _code(_read(PANEL / "main.swift"))
    monitor = _code(_read(PANEL / "KeyMonitor.swift"))
    assert "DetailTab" not in main
    assert "DetailTab" not in monitor
    triage = _code(_read(PANEL / "Triage.swift"))
    assert "DetailTab" not in triage
    assert "RailLayout.escapeRung(" in monitor


# --- the phone ---------------------------------------------------------------
# The phone gained its own container (`AgentScreen`) around `DetailTab`;
# the enum itself did not move.


def test_the_phone_screen_bar_is_its_own_file_scope_struct():
    src = _read(PHONE_DETAIL)
    assert src.count("struct PhoneAgentScreenBar") == 1
    view = _block(src, "struct AgentDetailView")
    start = src.index(view)
    bar_at = src.index("struct PhoneAgentScreenBar")
    assert not (start <= bar_at < start + len(view)), \
        "the screen bar must not be a member of AgentDetailView"
    bar = _code(_block(src, "struct PhoneAgentScreenBar: View"))
    assert "@Binding var screen: AgentScreen" in bar
    assert "ForEach(AgentScreen.chips(hosted:" in bar
    assert "item == .terminal" in bar
    assert "decryptFeedback?.cancel()" in bar
    assert "decryptFeedback?.begin(kind: .screen)" in bar
    # `.combine`, never `.ignore`: `.ignore` drops the press.
    assert ".accessibilityElement(children: .combine)" in bar
    assert ".accessibilityElement(children: .ignore)" not in bar
    # A floor, never a cap — and none of the three banned seams.
    assert ".frame(minHeight: 32)" in bar
    assert ".lineLimit(" not in bar
    assert ".system(size:" not in bar
    assert ".dynamicTypeSize(" not in bar


def test_the_phone_draws_the_tab_bar_and_forks_on_the_rule():
    src = _read(PHONE_DETAIL)
    view = _code(_block(src, "struct AgentDetailView"))
    assert "@State private var hostedTab: DetailTab = DetailTab.defaultTab" in view
    assert view.count("DetailTab.terminalAttached(") == 2, "terminalCover and syncWatch"
    body = _code(_block(src, "var body: some View"))
    assert "PhoneAgentScreenBar(screen: $screen" in body
    # The lead (still beside the name, the card and the origin) sits above
    # the bar, shared by both panes; the conversation sits under it.
    assert body.index("identity") < body.index("PhoneAgentScreenBar(")
    assert body.index("PhoneAgentScreenBar(") < body.index("ConversationScreen(")
    assert "conversationLead" not in body
    assert "header:" not in body   # the conversation carries no lead of its own
    assert "ConversationScreen(" in body
    assert "detailsScreen" in body
    details = _code(_block(src, "private var detailsScreen: some View"))
    assert "rest" in details
    assert "actions" in details
    assert "identity" not in details
    rest = _code(_block(src, "private var rest: some View"))
    assert "body(for: agent)" in rest
    assert "PhoneTerminalPane(" not in body
    cover = _code(_block(src, ".fullScreenCover(isPresented: terminalCover)"))
    assert "terminalScreen" in cover
    screen = _code(_block(src, "private var terminalScreen: some View"))
    assert "PhoneTerminalPane(" in screen
    assert "dismissTerminal()" in screen
    assert "client.watchTerminal(nil)" in cover
    dismiss = _code(_block(src, "private func dismissTerminal()"))
    assert "resignFirstResponder" in dismiss
    assert "hostedTab = .details" in dismiss
    assert "screen = .details" in dismiss


def test_the_phone_watch_follows_the_tab():
    src = _read(PHONE_DETAIL)
    watch = _code(_block(src, "private func syncWatch()"))
    assert "DetailTab.terminalAttached(hosted: hostedTerminal, tab: hostedTab)" in watch
    assert "client.watchTerminal(agent.sessionId)" in watch
    assert "client.watchTerminal(nil)" in watch
    view = _code(_block(src, "struct AgentDetailView"))
    assert ".onChange(of: hostedTab)" in view
    # The two old unconditional call sites are gone: every arrival asks the
    # helper, and the helper asks the tab.
    assert view.count("client.watchTerminal(agent.sessionId)") == 1


def test_the_phone_reply_box_comes_back_on_details():
    gate = _code(_block(_read(PHONE_DETAIL), "private var terminalInputHere: Bool"))
    assert "agent.ownTerminal && agent.canTerminalInput" in gate
    assert "client.snapshot.board.terminalStreamSupported" in gate
    assert "hostedTab == .terminal" in gate


def test_the_phone_message_document_lost_its_hosted_branch():
    src = _read(PHONE_DETAIL)
    assert src.count("else if agent.ownTerminal") == 0
    # Only the cast quote is capped (one line, shrinking to fit).
    assert src.count(".lineLimit(") == 1
    body = _code(_block(src, "private func body(for agent: Agent)"))
    assert "agent.ownTerminal" not in body


# --- nothing reached the daemon ----------------------------------------------


def test_the_disconnect_is_the_view_and_no_new_verb():
    """Not drawing the pane *is* the disconnect and the width hand-back. If
    a step had needed a daemon verb it would be the wrong step."""
    pane = _read(PANEL / "TerminalPane.swift")
    assert 'Panel.send(action: "panel_terminal"' in pane
    assert ".onDisappear" in pane
    for path in (DETAIL, TAB, PHONE_DETAIL):
        assert "panel_terminal" not in _read(path), path
        assert "terminal_phone_resize" not in _read(path), path


def test_the_contracts_state_the_tab_rule():
    claude = _read(ROOT / "CLAUDE.md")
    assert "TerminalPane fills what is left under the header" not in claude
    assert "DetailTab" in claude
    contract = _read(ROOT / "docs" / "phone-contract.md")
    assert "## The terminal is a real emulator, and it is the one exception" in contract
    assert "Details" in contract


# --- where a terminal the Mac is not drawing actually is ---------------------

WHEREABOUTS = "enum TerminalWhereabouts: Equatable {"


def test_terminal_whereabouts_is_one_table_on_both_clients():
    """An ad-hoc row (`origin_by == "adhoc"`) with no hosted terminal is a
    terminal Dark Army opened and no longer holds — never "find it in the
    editor yourself". The rule is pure, beside `DetailTab`, and the phone's
    copy is byte-equal outside comments."""
    panel = _squashed(_block(_read(TAB), WHEREABOUTS))
    phone = _squashed(_block(_read(PHONE_DETAIL), WHEREABOUTS))
    assert panel == phone
    for needle in ("static func of(originBy: String, hosted: Bool, tabGone: Bool = false) -> TerminalWhereabouts",
                   'originBy == "adhoc" ? .gone : .editor',
                   'case .gone: return "terminal gone"',
                   'case .lost: return "tab gone"',
                   'case .editor: return "runs in the editor"'):
        assert needle in panel, needle


def test_both_detail_panes_read_the_whereabouts_and_never_the_literal():
    """The literal editor wording lives in the table alone: a pane that
    wrote it by hand would draw it for a dead ad-hoc terminal again."""
    band = _code(_block(_read(DETAIL), "struct ElsewhereTerminalBand: View"))
    assert "TerminalWhereabouts.of(originBy: agent.originBy, hosted: agent.ownTerminal, tabGone: agent.tabGone)" in band
    assert "whereabouts.label" in band and "whereabouts.hint" in band
    assert "runs in the editor" not in band
    assert "find it in the editor yourself" not in band
    # The jump button is the editor's verb: absent on a gone terminal.
    assert "if agent.canJump && whereabouts == .editor {" in band
    phone = _code(_read(PHONE_DETAIL))
    phone_view = phone[phone.index("private var elsewhereTerminal"):
                       phone.index("private var elsewhereTerminal") + 2500]
    assert "TerminalWhereabouts.of(originBy: agent.originBy," in phone_view
    assert "tabGone: agent.tabGone" in phone_view
    assert "whereabouts.label" in phone_view
    assert "runs in the editor" not in phone_view
