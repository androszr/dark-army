"""The card an agent is on leads its detail header — source pins.

`plans/2026-09-06-card-title-in-agent-detail-header.md`. The rule itself is
table-tested in `panel/Tests/BobPanelTests/AgentCardLineTests.swift`; what
this file pins is what the Swift tests cannot see — that both headers draw
the line, that the phone's new lookup gates its refine rung the way the Mac's
does, that the two copies of the rule are spelled the same, that no line cap
crept in, and that the row's weaker `card_title` field was not read here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DETAIL = ROOT / "panel" / "Sources" / "BobPanel" / "AgentDetailPane.swift"
PANEL_VIEW = ROOT / "panel" / "Sources" / "BobPanel" / "PanelView.swift"
PHONE_MODELS = ROOT / "ios" / "BobPhone" / "Models.swift"
PHONE_DETAIL = ROOT / "ios" / "BobPhone" / "AgentDetailView.swift"

SIGNATURE = "static func cardLine(card: BoardCard?, sessionId: String)"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    """`text` with its comments removed, so a pin judges code and not prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_panel_header_takes_and_draws_the_board_card():
    src = _read(DETAIL)
    header = _code(_block(src, "struct AgentDetailHeader"))
    assert "var boardCard: BoardCard?" in header
    assert SIGNATURE in header
    assert '"Planning: "' in header
    assert "Text(titleLine)" in header
    pane = _code(_block(src, "struct AgentDetailPane"))
    assert "var boardCard: BoardCard?" in pane
    assert "boardCard: boardCard" in pane


def test_panel_caller_looks_the_card_up_once():
    src = _code(_read(PANEL_VIEW))
    assert "boardCard: bound" in src
    assert "bound == nil ? nil" in src
    # One resolution for the header and the chip alike: the old inline
    # `== nil` test on the lookup itself is gone. Two lookups in the file,
    # each for its own row: the detail pane's and the process row's (the
    # `card ⌗` chip on the table), never two for one row.
    assert ".card(forSession: row.agent.sessionId) == nil" not in src
    assert src.count(".card(forSession: row.agent.sessionId)") == 2
    assert _code(_block(src, "private func processRow")).count(
        ".card(forSession: row.agent.sessionId)") == 1


def test_phone_board_lookup_gates_the_refine_rung():
    src = _read(PHONE_MODELS)
    block = _code(_block(src, "func card(forSession id: String) -> BoardCard?"))
    assert "$0.sessionId == id" in block
    assert "$0.isRefining && $0.refineSessionId == id" in block
    assert block.index("$0.sessionId == id") < block.index("$0.isRefining")
    for line in block.splitlines():
        if "refineSessionId" in line:
            assert "isRefining" in line, line


def test_phone_detail_draws_and_opens_the_card():
    src = _read(PHONE_DETAIL)
    view = _code(_block(src, "struct AgentDetailView"))
    assert "board.card(forSession: agent.sessionId)" in view
    assert SIGNATURE in view
    assert '"Planning: "' in view
    assert "sheets.show(.card(card))" in view
    assert ".accessibilityLabel(cardLine.isEmpty" in view
    assert 'on card \\(cardLine)")' in view


def test_the_phone_draws_the_title_on_both_bodies():
    """Main draws the card in the lead; every other tab keeps the card's
    title as the one fixed line under the screen bar."""
    src = _read(PHONE_DETAIL)
    view = _code(_block(src, "struct AgentDetailView"))
    assert view.count("cardLead") >= 2, "declared and drawn on Main"
    assert "cardLead" in _code(_block(src, "private var mainScreen: some View"))
    body = _code(_block(src, "var body: some View"))
    assert body.index("PhoneAgentScreenBar(") < body.index("titleLine")
    title = _code(_block(src, "private var titleLine: some View"))
    assert "cardLine" in title and "sheets.show(.card(card))" in title


def test_the_rule_is_spelled_the_same_on_both_sides():
    def rule(path: Path) -> str:
        block = _block(_read(path), SIGNATURE)
        body = block[block.index("guard let card") : block.rindex("}")]
        return "\n".join(
            line.strip() for line in body.splitlines() if line.strip()
        )

    assert rule(DETAIL) == rule(PHONE_DETAIL)


def test_no_cap_was_added():
    # The card title is never capped. The one line limit on the phone is
    # the cast quote's: secondary, one line, shrinking to fit.
    assert _read(DETAIL).count(".lineLimit(") == 0, DETAIL
    phone = _read(PHONE_DETAIL)
    assert phone.count(".lineLimit(") == 1, PHONE_DETAIL
    assert ".lineLimit(1)" in phone.split("Text(quote)", 1)[1].split("}", 1)[0]


def test_the_phone_ignore_roster_did_not_grow():
    view = _read(PHONE_DETAIL)
    block = _block(view, "struct AgentDetailView")
    assert block.count(".accessibilityElement(children: .ignore)") == 2


def test_the_daemon_was_not_touched():
    for path, start in ((DETAIL, "struct AgentDetailHeader"),
                        (PHONE_DETAIL, "struct AgentDetailView")):
        assert "card_title" not in _code(_block(_read(path), start)), path
    # The Mac reads the row's `cardTitle` in one place too: the no-card
    # fallback (`titleLine`), the agents-list row's own order.
    mac = _code(_read(DETAIL))
    mline = _code(_block(_read(DETAIL), "private var titleLine: String"))
    assert mac.count("agent.cardTitle") == mline.count("agent.cardTitle") == 1
    # The phone reads the row's `cardTitle` in one place only: the
    # no-card fallback (`sessionLine`), never the linked lead.
    phone = _code(_read(PHONE_DETAIL))
    line = _code(_block(_read(PHONE_DETAIL), "private var sessionLine: String"))
    assert phone.count("agent.cardTitle") == line.count("agent.cardTitle") == 1


def test_the_phone_falls_back_to_the_session_name_without_a_card():
    """22 Sep 2026: a planning session whose card moved on to Backlog is on
    no card by the lookup's own gate, and the line above the tabs went
    blank. 23 Sep 2026: its name is then the `New session` placeholder, so
    the fleet row's own ladder leads — the daemon's `card_title`, else the
    session's name — as plain words, no link."""
    src = _read(PHONE_DETAIL)
    lead = _code(_block(src, "@ViewBuilder private var cardLead: some View"))
    assert "} else if !sessionLine.isEmpty {" in lead
    fallback = lead[lead.index("} else if !sessionLine.isEmpty {"):]
    assert "Text(sessionLine)" in fallback
    assert "DecryptButton" not in fallback and "sheets.show" not in fallback
    name = _code(_block(src, "private var sessionLine: String"))
    assert "agent.name" in name
    assert name.index("agent.cardTitle") < name.index("agent.name")
