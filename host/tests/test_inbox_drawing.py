"""How the inbox is *drawn*, pinned in `test_inbox_surface.py`'s shape.

Text pins over `InboxView.swift`: the panel is another language, so reading
the file off disk is the only cross-language guard there is. Every judgment
still lives in `Inbox.swift`, which this file never opens.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEW = (ROOT / "panel/Sources/BobPanel/InboxView.swift").read_text()


def test_the_link_idiom_is_gone_from_this_file():
    assert "buttonStyle(.link)" not in VIEW


def test_no_small_control_size():
    assert "controlSize(.small)" not in VIEW


def test_the_two_dismiss_presses_use_the_shared_outlined_style():
    assert VIEW.count("AlarmOutline(") == 2


def test_a_rule_exists_and_decoration_is_hidden_from_voiceover():
    assert "Theme.rule" in VIEW
    assert VIEW.count("accessibilityHidden(true)") >= 2


def test_bobs_line_is_drawn_only_where_bob_spoke():
    # Pinned by the exact expression: `Theme.dim` still inks nothing here,
    # so its mere absence would be the wrong assertion.
    assert "if !line.isEmpty {" in VIEW
    assert "refusal(item).isEmpty ? Theme.phosphor" in VIEW
    assert "nextAction" not in VIEW


def test_the_row_is_the_press_and_the_title_is_not_a_second_one():
    assert "contentShape(Rectangle())" in VIEW
    assert "Button { onOpen(item) } label:" not in VIEW
    assert '"Open"' not in VIEW


def test_the_inbox_routes_and_does_not_answer():
    """One verb on a row (Dismiss), one above the list (Dismiss all), and
    neither answers, moves, closes or deletes: every board verb left with
    the buttons that carried it."""
    for gone in ("markDone", "sendBack", "markChecked", "markReviewed",
                 "boardUpdate", "boardReset", "boardManualClear", "boardReview",
                 "doneArmed", "Acknowledge", "Clear all", "Inbox.clearable",
                 "blockingCount", "acknowledgeAlert", "accessAlertAck"):
        assert gone not in VIEW, gone
    assert VIEW.count("client.inboxAck(") == 1
    assert '"Dismiss"' in VIEW
    assert '"Dismiss all"' in VIEW
    assert "onChange(of: dismissable.count)" in VIEW
    assert "item.wire.dismissable" in VIEW
    assert "client.snapshot.inbox.available" in VIEW
    assert "write(refusalSlot(item))" in VIEW


def test_one_number_and_a_heading_only_where_there_are_two_projects():
    assert '"\\(items.count) WAITING ON YOU"' in VIEW
    assert "BLOCKING" not in VIEW
    assert "if groups.count > 1 { heading(group) }" in VIEW


def test_the_rail_measures_nothing():
    assert "GeometryReader" not in VIEW


def test_the_band_the_inbox_replaced_is_not_named_here():
    assert "NeedsYouCardsBand" not in VIEW
