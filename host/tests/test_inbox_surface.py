"""The inbox reads the daemon's judgments and re-derives neither.

Text pins in `test_needs_you_cards.py`'s shape: the panel is another language,
so the only cross-language guard available is to read both files off disk.
"""
import re
from pathlib import Path

from dark_army_daemon import board

ROOT = Path(__file__).resolve().parents[2]
INBOX = (ROOT / "panel/Sources/BobPanel/Inbox.swift").read_text()


def test_a_ready_plan_and_a_closed_card_are_not_inbox_subjects():
    """Nothing FYI is listed (20 Sep 2026): a plan nobody has started is the
    Backlog tab's, a closed card awaiting review is the Done column's, a
    burst alert is the access log's. The inbox reads none of their fields."""
    for field in ("planPath", "refineState", "awaitsReview", "closedBy",
                  "AccessAlert", "planReady", "accessBurst", "awaitingReview"):
        assert field not in INBOX, field


def test_plan_path_still_has_one_writer_outside_the_field_allow_list():
    assert board.SINGLE_WRITER["plan_path"] == "attach_plan"
    assert "plan_path" not in board.BoardStore._WRITABLE


def test_three_kinds_named_by_what_you_do_over_five_wire_names():
    assert "case answer = 0" in INBOX
    assert "case look\n" in INBOX
    assert "case stopped\n" in INBOX
    for wire in ("permission", "question", "ended_work", "manual_check", "waiting"):
        assert f'return "{wire}"' in INBOX
    # Every entry blocks somebody: no tag, no FYI, no per-kind sentence.
    for gone in ("NEEDS YOU", "FYI", "nextAction", "blockingCount", "clearable"):
        assert gone not in INBOX, gone
    assert "var dismissable: Bool { self != .permission }" in INBOX


def test_the_published_flags_are_consumed():
    assert "needsYou" in INBOX
    assert "manualCheckDue" in INBOX


def test_neither_daemon_judgment_is_re_derived():
    assert not re.search(r'linkState\s*==\s*"ended"', INBOX)
    assert "manualSteps.isEmpty" not in INBOX


def test_the_band_the_inbox_replaced_is_gone():
    panel = ROOT / "panel/Sources/BobPanel"
    for path in panel.glob("*.swift"):
        assert "NeedsYouCardsBand" not in path.read_text(), path.name


PANEL_VIEW = (ROOT / "panel/Sources/BobPanel/PanelView.swift").read_text()


def test_the_inbox_is_dated_from_the_fleet_sections_own_stamp():
    """A slim frame omits an unchanged `agents` section, so a row's
    `idleSeconds` is as old as the last frame that carried one. Paired with the
    frame's own clock, `waitingSince` would slide forward a second per second
    and the age `InboxView` draws would freeze — the one frozen-clock bug this
    file can catch without a Swift toolchain."""
    assert "now: snapshot.agentsStamp" in PANEL_VIEW
    assert "now: snapshot.generatedAt" not in PANEL_VIEW


def test_the_inbox_names_no_wall_clock_of_its_own():
    """`Inbox.items` is a pure function of the rows and the stamp it is handed;
    a `Date()` inside it would be a second, unauditable clock."""
    assert "Date()" not in INBOX
    assert "timeIntervalSince1970" not in INBOX


PHONE_INBOX = (ROOT / "ios/BobPhone/Inbox.swift").read_text()


def test_the_inbox_reads_the_report_headline_and_parses_nothing():
    """A stopped agent's detail and an ended card's detail read the daemon's
    one-line `workReport?.headline`; neither inbox ever looks inside the
    report text itself (`work_report.py` did that once, on the Mac)."""
    for name, text in (("panel", INBOX), ("phone", PHONE_INBOX)):
        for literal in ("## Work done", "lastReport"):
            assert literal not in text, (name, literal)
        # As words: `case .startAsked:` is a wire kind, not a report label.
        for label in ("Asked:", "Unchecked:"):
            assert not re.search(r"(?<![A-Za-z])" + label, text), (name, label)
        assert "workReport?.headline" in text, name
    assert INBOX.count("workReport") >= 2
