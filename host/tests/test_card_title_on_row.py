"""A card-bound session's fleet row wears its card's title.

The join lives in the daemon — a judgment goes there exactly when it needs
evidence the clients do not have on the wire, and neither the panel nor the
phone has a session-to-card join. These tests pin the join, the fallback rule
that makes it a fallback rather than an override, and that the clients read
the one key the daemon publishes rather than re-deriving the judgment.
"""

import re
from pathlib import Path

from dark_army_daemon.daemon import UNNAMED_SESSION, BobDaemon

ROOT = Path(__file__).resolve().parents[2]
DAEMON_PY = ROOT / "host" / "dark_army_daemon" / "daemon.py"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"

SID = "00000000-0000-0000-0000-000000000000"


def _row(cards, **stub_extra):
    """One enriched row for an otherwise nameless session, against `cards`."""
    d = BobDaemon()
    d._board_state = {"cards": cards}
    stub = {"session_id": SID, "project": "proj", "state": "idle",
            "subagents": 0, "subagent_ids": [], "_category": "sleeping"}
    stub.update(stub_extra)
    return d._enrich_agent_stubs([stub])["sleeping"][0]


def _card(**fields):
    card = {"id": "c1", "title": "", "column_name": "prep", "session_id": "",
            "link_state": "", "refine_session_id": "", "refine_state": ""}
    card.update(fields)
    return card


def test_a_refining_session_wears_its_cards_title():
    row = _row([_card(title="Fix the strip", refine_session_id=SID,
                      refine_state="live")])
    assert row["card_title"] == "Fix the strip"
    # A fallback, not a rename: `name` is what it always was.
    assert row["name"] == UNNAMED_SESSION


def test_a_started_session_wears_its_cards_title():
    row = _row([_card(title="Fix the strip", session_id=SID,
                      link_state="live", column_name="in_progress")])
    assert row["card_title"] == "Fix the strip"
    assert row["name"] == UNNAMED_SESSION


def test_a_real_name_is_never_overridden():
    row = _row([_card(title="Fix the strip", session_id=SID,
                      link_state="live")],
               cli_name="What I called it")
    assert row["name"] == "What I called it"
    assert "card_title" not in row


def test_no_card_means_no_key():
    row = _row([])
    assert row["name"] == UNNAMED_SESSION
    assert "card_title" not in row


def test_the_live_card_wins_over_a_stale_session_id():
    """`update()` does not clear `session_id` when a card leaves In progress,
    so a dead link must never outrank a live one — in either snapshot order."""
    stale = _card(id="stale", title="Old work", session_id=SID,
                  link_state="ended")
    live = _card(id="live", title="Real work", session_id=SID,
                 link_state="live")
    assert _row([stale, live])["card_title"] == "Real work"
    assert _row([live, stale])["card_title"] == "Real work"


def test_a_blank_title_publishes_nothing():
    for title in ("", "   ", "\n\t"):
        row = _row([_card(title=title, session_id=SID, link_state="live")])
        assert "card_title" not in row, f"published for title {title!r}"


def test_whitespace_in_a_title_is_collapsed():
    row = _row([_card(title="  Fix   the\nstrip ", session_id=SID,
                      link_state="live")])
    assert row["card_title"] == "Fix the strip"


def test_the_daemon_has_the_only_write_site():
    text = DAEMON_PY.read_text()
    # Counted precisely rather than by the bare key name: a **subagent** row
    # carries its own `card_title` — the parent session's origin card, a
    # different object and a different question (`origin.py`). The invariant
    # here is about the row.
    assert text.count('entry["card_title"]') == 1, (
        "the row's card title must be written in exactly one place")
    for folder in (PANEL, PHONE):
        for path in folder.rglob("*.swift"):
            assert UNNAMED_SESSION not in path.read_text(), (
                f"{path} learned the string {UNNAMED_SESSION!r} — the "
                '"is this row unnamed?" judgment belongs to the daemon')


def _command_body(text: str) -> str:
    # The Mac's ladder is `static func baseTitle(for:cardLine:)`, shared by
    # the column's one-line and two-line forms; the phone's is still the
    # private property.
    match = re.search(r"(?:private var command: String|"
                      r"static func baseTitle\(for agent: Agent[^)]*\) -> String) \{"
                      r"(.*?)\n    \}", text, re.S)
    assert match, "no `command` ladder found"
    return match.group(1)


def test_the_panel_reads_the_card_title_first():
    models = (PANEL / "Models.swift").read_text()
    assert "var cardTitle" in models
    assert 'case cardTitle = "card_title"' in models
    assert 'cardTitle = c.value(.cardTitle, "")' in models, (
        "an older daemon's frame must decode to an empty string, not throw")
    body = _command_body((PANEL / "ProcessTable.swift").read_text())
    assert "agent.cardTitle" in body
    # The detail's own card line leads, so the row names what opens.
    assert "cardLine" in body
    assert body.index("cardLine") < body.index("agent.cardTitle")
    assert body.index("agent.cardTitle") < body.index("agent.name"), (
        "a cardTitle rung below `name` is dead code: the daemon publishes it "
        "only beside a non-empty name")


def test_the_phone_reads_the_card_title_first():
    models = (PHONE / "Models.swift").read_text()
    assert "var cardTitle" in models
    assert 'case cardTitle = "card_title"' in models
    assert 'cardTitle = c.value(.cardTitle, "")' in models
    body = _command_body((PHONE / "ProcessTable.swift").read_text())
    assert "agent.cardTitle" in body
    # The detail's own card line leads, so the row names what opens.
    assert "cardLine" in body
    assert body.index("cardLine") < body.index("agent.cardTitle")
    assert body.index("agent.cardTitle") < body.index("agent.name")
