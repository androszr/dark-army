# host/tests/test_card_sections.py
"""The card detail's per-stage sections — source pins over the Swift.

`plans/2026-09-12-card-detail-folds-per-column.md`. The rule itself —
which stage a card is in, the order of its sections, the leads, the pinned
four and the closed-row words — is tabled in
`panel/Tests/BobPanelTests/CardSectionsTests.swift`. What this file pins is
what the Swift tests cannot see: that the phone's copy of `CardSections` is
written exactly as the panel's, that both card screens draw through it and
re-derive no stage, that the open set is screen state on both and on no
persisted shape, that the fold row is at touch measure and spoken, and that
no daemon file has heard of any of it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
RULE = PANEL / "CardSections.swift"
PANEL_VIEW = PANEL / "BoardCardSheet.swift"
PHONE_VIEW = PHONE / "CardDetailView.swift"
SWIFT_TESTS = ROOT / "panel" / "Tests" / "BobPanelTests" / "CardSectionsTests.swift"
DAEMON = ROOT / "host" / "dark_army_daemon"

ENUM = "enum CardSections {"


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


# --- the rule lives in one place, spelled the same on both sides -------------


def test_the_rule_is_pure_and_lives_in_its_own_file():
    src = _read(RULE)
    block = _code(_block(src, ENUM))
    for needle in ("enum Section: String, CaseIterable {",
                   "enum Stage: String, CaseIterable {",
                   "static func stage(column: String, linkState: String,",
                   "static let pinned: Set<Section> = [.status, .verbs, .queue, .dependencies]",
                   "static func order(for stage: Stage) -> [Section]",
                   "static func leads(for stage: Stage) -> Set<Section>",
                   "static func isOpen(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool",
                   "struct Facts: Equatable {",
                   "static func summary(_ s: Section, facts: Facts) -> String",
                   'static let statusLine = "Finished — waiting on your check"'):
        assert needle in block, needle
    # Rules, not views: Foundation only, and nothing here spins.
    assert "import SwiftUI" not in _code(src)
    assert "ProgressView" not in src
    # The tool is not an input. The signature has nowhere to put it.
    assert "tool" not in _code(_block(block, "static func stage("))


def test_the_six_stages_and_every_section_label_are_tabled():
    block = _code(_block(_read(RULE), ENUM))
    assert "case prep, backlog, running, manualCheck, ended, done" in block
    for label in ('case status = "STATUS"', 'case manualCheck = "MANUAL CHECK"',
                  'case workRecord = "WHAT CHANGED"', 'case editor = "EDIT CARD"',
                  'case danger = "DELETE"', 'case plan = "PLAN"',
                  'case timeline = "TIMELINE"'):
        assert label in block, label


def test_the_phone_copy_is_written_exactly_as_the_panels():
    panel = _squashed(_block(_read(RULE), ENUM))
    phone = _squashed(_block(_read(PHONE_VIEW), ENUM))
    assert panel == phone


def test_a_doctored_copy_is_noticed():
    """Anti-vacuous: the comparison above is not satisfied by any two blocks
    that merely start the same way."""
    panel = _block(_read(RULE), ENUM)
    doctored = panel.replace('case danger = "DELETE"', 'case danger = "REMOVE"', 1)
    assert doctored != panel
    assert _squashed(doctored) != _squashed(panel)


def test_the_rule_is_tabled_in_swift():
    src = _read(SWIFT_TESTS)
    for needle in ("CardSections.Stage.allCases.count, 6",
                   'CardSections.stage(column: "in_progress"',
                   "CardSections.order(for:",
                   "CardSections.summary(",
                   "CardSections.isOpen(",
                   "CardSections.leads(for:"):
        assert needle in src, needle
    # Every stage is named at least once in the table.
    for stage in ("prep", "backlog", "running", "manualCheck", "ended", "done"):
        assert f".{stage}" in src, stage


# --- both views read the rule and re-derive none of it ----------------------


def test_both_views_hold_the_open_set_as_screen_state_only():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    assert panel.count("@State private var openedSections: Set<CardSections.Section> = []") == 1
    assert phone.count("@State private var openedSections: Set<CardSections.Section> = []") == 1
    # Kept nowhere else: not in the router's per-card draft, not on disk.
    for path in (PHONE / "PhoneSheetHost.swift", PHONE / "CardCache.swift",
                 PANEL / "Drafts.swift"):
        text = _read(path)
        assert "openedSections" not in text, path.name
        assert "CardSections" not in text, path.name


def test_both_views_reset_the_open_set_on_card_swap_and_stage_change():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    swap = _block(panel, '.onChange(of: live?.id ?? "")')
    assert "openedSections = []" in swap
    assert ".onChange(of: stage)" in panel
    assert "openedSections = []" in _block(panel, ".onChange(of: stage)")
    assert "openedSections = []" in _block(phone, ".onChange(of: seed.id)")
    assert ".onChange(of: stage) { _, _ in openedSections = [] }" in phone


def test_both_views_draw_through_the_order_and_ask_the_stage_once():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    for text, name in ((panel, "panel"), (phone, "phone")):
        assert text.count("ForEach(CardSections.order(for: stage), id: \\.self)") == 1, name
        assert text.count("CardSections.stage(") == 1, name
        assert "CardSections.isOpen(" in text, name
        assert "CardSections.rowText(" in text, name
    # The Mac's body is the rule's, not a fixed list.
    body = _block(panel, "var body: some View {")
    assert "ForEach(CardSections.order" in body
    assert "queueSection\n" not in body
    assert "manualCheckSection\n" not in body
    assert "dangerZone\n" not in body
    # The composer keeps its form and reads no rule.
    assert "if isComposer {" in body
    assert body.index("if isComposer {") < body.index("ForEach(CardSections.order")


def test_neither_view_re_derives_a_stage():
    """`CardSections.stage` is the one reading of `linkState == "ended"` and
    of `manualCheckDue` for *where the card is*. The views may still read
    both for other things (`isBusy`, the badge), but not to pick an order."""
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    phone_view = phone[: phone.index(ENUM)]
    assert 'linkState == "ended"' not in _block(phone_view, "var body: some View {")
    assert 'linkState == "ended"' not in _block(panel, "var body: some View {")
    for text in (panel, phone_view):
        stage = _block(text, "private var stage: CardSections.Stage {")
        assert "CardSections.stage(" in stage
        assert "column" in stage and "linkState" in stage and "manualCheckDue" in stage


def test_the_status_line_is_the_rules_on_both_sides():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    assert panel.count("CardSections.statusLine") == 1
    assert phone.count("CardSections.statusLine") == 1
    assert "CardSections.statusLine" in _block(panel, "private var statusSection: some View {")
    assert "CardSections.statusLine" in _block(phone, "private var statusSection: some View {")
    # Neither side words it itself.
    assert '"Finished — waiting on your check"' not in panel
    assert '"Finished — waiting on your check"' not in phone[: phone.index(ENUM)]


def test_a_fold_row_is_a_header_that_says_whether_it_is_open():
    panel = _code(_block(_read(PANEL_VIEW), "private struct CardFoldRow: View {"))
    phone = _code(_block(_read(PHONE_VIEW), "struct PhoneCardFoldRow: View {"))
    for row in (panel, phone):
        assert ".accessibilityLabel(label)" in row
        assert '.accessibilityValue(open ? "shown" : "hidden")' in row
        assert ".accessibilityAddTraits(.isHeader)" in row
        assert ".buttonStyle(.plain)" in row
    # The phone's is at touch measure, with a hint; the Mac's takes the cursor.
    assert ".frame(minHeight: 44, alignment: .leading)" in phone
    assert ".accessibilityHint(" in phone
    assert ".clickable()" in panel


def test_the_phone_moved_delete_out_of_the_acting_row():
    phone = _code(_read(PHONE_VIEW))
    verbs = _block(phone, "private var verbs: some View {")
    assert "deleteLabel" not in verbs
    assert "boardDelete" not in verbs
    danger = _block(phone, "private var dangerSection: some View {")
    assert "DecryptButton(deleteLabel)" in danger
    assert "PhoneActions.boardDelete" in danger
    # And Delete is the last section of every order.
    rule = _code(_block(_read(RULE), ENUM))
    order = _block(rule, "static func order(for stage: Stage) -> [Section] {")
    returns = re.findall(r"return \[([^\]]*)\]", order, flags=re.S)
    assert len(returns) == 6
    for r in returns:
        assert r.split(",")[-1].strip() == ".danger", r
        assert r.split(",")[0].strip() == ".status", r


def test_the_phone_keeps_its_text_rules():
    phone = _read(PHONE_VIEW)
    assert ".lineLimit(" not in phone
    assert "ProgressView" not in phone
    assert ".system(size:" not in phone
    assert ".dynamicTypeSize(" not in phone
    assert phone.count("frame(minHeight: 44") >= 3


def test_a_pinned_section_never_draws_a_chevron():
    """A chevron is a promise. `foldedSection` draws the body straight for a
    section the rule opens with nothing pressed, and a row only otherwise."""
    for path, row in ((PANEL_VIEW, "CardFoldRow("), (PHONE_VIEW, "PhoneCardFoldRow(")):
        text = _code(_read(path))
        fold = _block(text, "private func foldedSection(_ s: CardSections.Section) -> some View {")
        assert "if CardSections.isOpen(s, stage: stage, opened: []) {" in fold
        assert "if hasContent(s) {" in fold
        assert row in fold
        assert fold.index("if CardSections.isOpen(s, stage: stage, opened: []) {") < fold.index(row)


# --- the daemon has never heard of it ---------------------------------------


def test_no_daemon_file_mentions_the_rule():
    hits = []
    for path in sorted(DAEMON.rglob("*.py")):
        text = path.read_text()
        if "CardSections" in text or "openedSections" in text:
            hits.append(path.name)
    assert hits == []
    # Nor the menu bar, which never reaches the card window.
    for path in sorted((ROOT / "host" / "dark_army_menubar").rglob("*.py")):
        text = path.read_text()
        assert "CardSections" not in text, path.name


# --- the lead five, then one MORE row ----------------------------------------
# `plans/2026-09-20-simplify-card-details.md`: nothing sits above the MORE
# row but the pinned three and the two stated exceptions; the one next action
# and the state sentence are the rule's; Collaboration is a section of the
# rule rather than a second fold outside it.


def test_the_three_new_labels_are_tabled_on_both_sides():
    for path in (RULE, PHONE_VIEW):
        block = _code(_block(_read(path), ENUM))
        for label in ('case collaboration = "COLLABORATION"',
                      'case otherVerbs = "OTHER ACTIONS"',
                      'case more = "MORE"'):
            assert block.count(label) == 1, (path.name, label)
        assert "static func nextAction(stage: Stage, reach: Reach) -> NextAction" in block
        assert "static func stateWords(for stage: Stage) -> String" in block
        assert "static func hidden(for stage: Stage) -> [Section]" in block
        assert "static func shown(_ s: Section, stage: Stage, opened: Set<Section>) -> Bool" in block
        assert "struct Reach: Equatable {" in block
        assert "enum NextAction: String {" in block
        assert "var hidden = 0" in block


def test_more_is_in_every_order_once_and_is_neither_pinned_nor_a_lead():
    rule = _code(_block(_read(RULE), ENUM))
    order = _block(rule, "static func order(for stage: Stage) -> [Section] {")
    returns = re.findall(r"return \[([^\]]*)\]", order, flags=re.S)
    assert len(returns) == 6
    for r in returns:
        assert r.count(".more") == 1, r
    pinned = next(line for line in rule.splitlines() if "static let pinned" in line)
    assert ".more" not in pinned
    leads = _block(rule, "static func leads(for stage: Stage) -> Set<Section> {")
    assert ".more" not in leads
    # `hidden` filters the row out by name, or it would hand itself back.
    hidden = _block(rule, "static func hidden(for stage: Stage) -> [Section] {")
    assert "$0 != .more" in hidden


def test_both_views_ask_the_rule_for_the_next_action_and_the_state_once():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    phone_view = phone[: phone.index(ENUM)]
    for text, name in ((panel, "panel"), (phone_view, "phone")):
        assert text.count("CardSections.nextAction(") == 1, name
        assert text.count("CardSections.stateWords(") == 1, name
        assert "CardSections.stateWords(" in _block(text, "private var statusSection: some View {"), name
        assert "CardSections.shown(s, stage: stage, opened: openedSections)" in _block(
            text, "private func foldedSection(_ s: CardSections.Section) -> some View {"), name
        # More rides `openedSections`; no second slot.
        assert "@State private var moreOpen" not in text, name


def test_collaboration_is_a_section_of_the_rule_on_both_sides():
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    for text, name in ((panel, "panel"), (phone, "phone")):
        assert text.count("CollaborationView(") == 1, name
        body = _block(text, "var body: some View {")
        assert "CollaborationView(" not in body, name
        section = _block(text, "private func section(_ s: CardSections.Section) -> some View {")
        assert "CollaborationView(" in section, name
        assert "initiallyExpanded: true" in section, name


def test_the_phones_acting_row_is_the_bands_one_verb():
    phone = _code(_read(PHONE_VIEW))
    verbs = _block(phone, "private var verbs: some View {")
    assert "switch nextAction" in verbs
    # Every other verb is behind MORE, in its own section.
    other = _block(phone, "private var otherVerbs: some View {")
    assert "startHereButton" in other
    assert "startHereLabel" in _block(phone, "private var startHereButton: some View {")
    assert "START WHEN PLANNED" in other
    assert "startHereLabel" not in verbs
    assert "startHereButton" not in verbs
    assert "START WHEN PLANNED" not in verbs


def test_the_mac_editor_draws_the_assistant_row_only_for_the_composer():
    """The saved card's assistant row is in `identityLine`; the block in
    `fields` is wrapped in `if isComposer {` so the editor under MORE does
    not draw it a second time. Two call sites, both read by their opening
    line (`test_provider_switcher.py`, `test_composer_phase.py`)."""
    panel = _code(_read(PANEL_VIEW))
    assert panel.count('field("Assistant", "Who takes it when it starts.") {') == 2
    identity = _block(panel, "private var identityLine: some View {")
    assert 'field("Assistant", "Who takes it when it starts.") {' in identity
    fields = _block(panel, "private var fields: some View {")
    at = fields.index('field("Assistant", "Who takes it when it starts.") {')
    before = fields[:at]
    assert before.rstrip().endswith("if isComposer {"), before[-80:]


# --- the repair round: the stage hears run_active, the lead says who has it -
# `plans/2026-09-20-simplify-card-details.md`, bug-audit findings 1-6.


def test_the_stage_reads_run_active_from_both_views():
    """`run_active` is the pipeline band's RUN/ENDED split; the card screen
    reads the same field into the rule, so a `live` card whose session Dark Army
    can no longer hear is not drawn as "working" while the band draws it
    ENDED. The daemon's own name, decoded already, never re-derived."""
    rule = _code(_block(_read(RULE), ENUM))
    stage = _block(rule, "static func stage(")
    assert "runActive: Bool" in stage
    assert "!linkState.isEmpty && !runActive" in stage
    words = _block(rule, "static func stateWords(for stage: Stage, linked: Bool) -> String {")
    assert '"In progress — nobody working on it yet"' in words
    assert '"An assistant is working on it"' in words
    panel = _code(_read(PANEL_VIEW))
    phone = _code(_read(PHONE_VIEW))
    for text, card in ((panel, "live"), (phone, "card")):
        stage = _block(text, "private var stage: CardSections.Stage {")
        assert f"runActive: {card}.runActive" in stage, card
        status = _block(text, "private var statusSection: some View {")
        assert f"linked: !{card}.linkState.isEmpty" in status, card
    for text in (panel, phone[: phone.index(ENUM)]):
        assert '"nobody working on it"' not in text


def test_the_phone_lead_names_the_assistant_once_the_switcher_is_gone():
    phone = _code(_read(PHONE_VIEW))
    body = _block(phone, "private var identityLead: some View {")
    at = body.index("assistantPicker")
    assert "} else if !card.tool.isEmpty {" in body[at:at + 200]
    assert "assistantRecord" in body[at:at + 260]
    record = _block(phone, "private var assistantRecord: some View {")
    assert "PhoneProviderMark(provider: card.tool" in record
    assert "PhoneProviderMark.label(for: card.tool)" in record
    assert 'accessibilityLabel("Assistant")' in record
    assert "Menu {" not in record and "Button" not in record


def test_the_phone_messages_row_is_absent_while_the_band_draws_the_box():
    phone = _code(_read(PHONE_VIEW))
    has = _block(phone, "private func hasContent(_ s: CardSections.Section) -> Bool {")
    assert "case .thread: return canMessageSession && nextAction != .reply" in has
    section = _block(phone, "private var messageSection: some View {")
    assert "if canMessageSession, nextAction != .reply {" in section


def test_the_phone_band_confirms_an_arm_raised_off_screen():
    """The column mover under EDIT CARD arms Done / Start and a plan-gate
    refusal after a move arms Start; their confirming button lives in
    OTHER ACTIONS, a closed row under MORE, and `Arm.timeoutSeconds` would
    disarm it before a person found it. The band draws the armed button
    while that row is not on screen."""
    phone = _code(_read(PHONE_VIEW))
    verbs = _block(phone, "private var verbs: some View {")
    assert "armedElsewhere" in verbs
    assert verbs.index("armedElsewhere") < verbs.index("switch nextAction")
    armed = _block(phone, "private var armedElsewhere: some View {")
    assert "if armedOffScreen {" in armed
    for button in ("startButton", "startHereButton", "doneArmedButton"):
        assert button in armed, button
    gate = _block(phone, "private var armedOffScreen: Bool {")
    assert "!otherVerbsOnScreen" in gate
    for slot in ("arm.start != nil", "arm.startHere != nil", "arm.done != nil"):
        assert slot in gate, slot
    on_screen = _block(phone, "private var otherVerbsOnScreen: Bool {")
    assert "CardSections.shown(.otherVerbs, stage: stage, opened: openedSections)" in on_screen
    assert "CardSections.isOpen(.otherVerbs, stage: stage, opened: openedSections)" in on_screen
    has = _block(phone, "private func hasContent(_ s: CardSections.Section) -> Bool {")
    assert ("case .verbs: return (nextAction != .none && !(nextAction == .done && showsDoneClose))"
            " || armedOffScreen") in has
    # The same three buttons, once each, in OTHER ACTIONS.
    other = _block(phone, "private var otherVerbs: some View {")
    for button in ("startHereButton", "doneArmedButton"):
        assert other.count(button) == 1, button


def test_the_mac_lead_assistant_row_is_the_tiles_and_writes_at_once():
    """START and Refine act on `live.tool`, so the lead's row edits the live
    card through `boardUpdate` like the tile — never the footer's draft —
    and is gated on `canRetool` with an inert record once a session is
    bound. The composer's draft copy in `fields` is untouched."""
    panel = _code(_read(PANEL_VIEW))
    identity = _block(panel, "private var identityLine: some View {")
    lead = _block(identity, 'field("Assistant", "Who takes it when it starts.") {')
    assert "state.draft.tool" not in lead
    assert "if canRetool(live) {" in lead
    assert "pick: { setTool(live, $0) }" in lead
    assert "ProviderSwitch.recordMark(selected: live.tool)" in lead
    assert "interactive: false" in lead
    assert "if canRetool(live) || !live.tool.isEmpty {" in identity
    fields = _block(panel, "private var fields: some View {")
    draft = _block(fields, 'field("Assistant", "Who takes it when it starts.") {')
    assert "pick: { state.draft.tool = $0 }" in draft
    retool = _block(panel, "private func canRetool(_ live: BoardCard) -> Bool {")
    assert "live.sessionId.isEmpty && !live.isDispatching" in retool
    set_tool = _block(panel, "private func setTool(_ live: BoardCard, _ tool: String) {")
    assert "state.disarm()" in set_tool
    # Save sends `draft.tool` at `draft.revision`; this write moved both.
    assert "state.draft.tool = tool" in set_tool


def test_the_mac_lead_assistant_pick_is_guarded_and_adopts_the_replys_revision():
    """`setTool` is a `board_update` like Save, so it carries
    `expected_revision` at the draft's own number — a pick against a card
    the phone changed is refused in `CARD_CHANGED_REFUSAL`'s words, arming
    Save anyway, and the draft is left alone. A landed pick adopts the
    **reply's** `revision`, never the snapshot's `now.revision`: that is
    the card's latest number, whatever anyone wrote since the window
    opened, and adopting it would walk the open-time title past the
    daemon's guard on the next Save. The wire is `outcomeWrite`, the one
    seam that parses `revision`; `post()` / `boardUpdate` do not."""
    panel = _code(_read(PANEL_VIEW))
    set_tool = _block(panel, "private func setTool(_ live: BoardCard, _ tool: String) {")
    assert 'client.outcomeWrite(' in set_tool
    assert 'action: "board_update", cardId: live.id' in set_tool
    assert '"expected_revision": String(state.draft.revision)' in set_tool
    assert "client.boardUpdate(" not in set_tool
    # The refusal arms Save anyway and touches no draft field.
    refused = _block(set_tool, "if result.isCardChangedRefusal {")
    assert "cardChangedAt = result.currentRevision" in refused
    assert "state.draft" not in refused
    assert "return" in refused
    # Success adopts the reply's number and clears the arm there only.
    assert "state.draft.revision = revision" in set_tool
    assert "if let revision = result.cardRevision {" in set_tool
    assert "now.revision" not in set_tool
    assert set_tool.count("cardChangedAt = nil") == 1
    assert set_tool.index("guard result.ok else { return }") < set_tool.index("cardChangedAt = nil")
    assert set_tool.index("state.draft.tool = tool") < set_tool.index("cardChangedAt = nil")
    # `draft.tool` moves only off the reply, so a Save pressed during the
    # round-trip cannot re-send the old assistant as "Save anyway".
    assert set_tool.index("guard result.ok else { return }") < set_tool.index("state.draft.tool = tool")


def test_closing_the_card_window_disarms_the_tiles_slots():
    """Start / Done armed in the window ride `state.armed` / `doneArmed`,
    the slots the board tile reads; a close that left them standing would
    leave the tile asking "Really start?" for a press nobody can see."""
    text = _code(_read(PANEL / "BoardState.swift"))
    close = _block(text, "func closeEditor() {")
    assert "disarm()" in close
    assert close.index("disarm()") < close.index("editing = nil")
    disarm = _block(text, "func disarm() {")
    for slot in ("armed = nil", "doneArmed = nil", "armedHere = nil"):
        assert slot in disarm, slot


# --- CHANGES (review and merge) is tabled in both copies ----------------------------


def test_changes_is_a_folded_section_in_both_copies():
    """`plans/2026-10-03-review-and-merge-done-card.md`: CHANGES sits behind
    MORE in the Done and ended orders, is never a lead or pinned, and is the
    same line in the panel's rule and the phone's copy."""
    for src in (_read(RULE), _read(PHONE_VIEW)):
        block = _code(_block(src, ENUM))
        assert 'case changes = "CHANGES"' in block
        assert block.count('case changes = "CHANGES"') == 1
        assert ".workRecord, .changes, .run," in " ".join(block.split())
    panel = _code(_read(RULE))
    ended = _block(panel, "case .ended:\n            return [")
    assert ".changes" in ended and ended.index(".more") < ended.index(".changes")
    done = _block(panel, "case .done:\n            return [")
    assert done.index(".more") < done.index(".changes")
    assert ("static let pinned: Set<Section> = "
            "[.status, .verbs, .queue, .dependencies]") in panel
    leads = _block(panel, "static func leads(for stage: Stage)")
    assert ".changes" not in leads
