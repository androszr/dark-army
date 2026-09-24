"""The process table's rows carry the detail header's two chips.

`⌗` puts the session's card in front of the reader on the board and `↗`
raises its terminal — reachable from the Active list itself, without opening
the detail pane. Each is absent, never inert: `⌗` only where the session is
bound to a card, `↗` only where `isJumpable` says there is a terminal to
raise. Both slots are reserved on every row and in the header so the CTX
column keeps its edge.
"""
import re
from pathlib import Path

PANEL = Path(__file__).resolve().parents[2] / "panel" / "Sources" / "BobPanel"


def _read(name: str) -> str:
    return (PANEL / name).read_text()


def _block(text: str, start: str) -> str:
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


def test_the_row_takes_both_chips_as_optional_actions():
    row = _block(_read("ProcessTable.swift"), "struct ProcessRow")
    assert "var onShowCard: (() -> Void)? = nil" in row
    assert "var onJump: (() -> Void)? = nil" in row
    assert 'chip("⌗", action: onShowCard' in row
    assert 'chip("↗", action: onJump' in row
    assert "Jump to terminal" in row
    assert "Show this session's card on the board" in row
    # A chip with no action draws its empty slot, never a disabled button.
    assert "Color.clear.frame(width: Self.chipSize, height: Self.chipSize)" in row
    assert ".disabled(" not in row


def test_the_header_reserves_the_two_slots():
    header = _block(_read("ProcessTable.swift"), "struct ProcessTableHeader")
    assert header.count("Color.clear.frame(width: ProcessRow.chipSize") == 2
    assert header.index("ProcessRow.chipSize") < header.index('Text("CTX")')


def test_the_panel_gates_each_chip_on_the_daemons_word():
    view = _read("PanelView.swift")
    body = re.search(r"private func processRow\(_ row: SectionRow\) -> some View \{(.*?)\n    \}",
                     view, re.S).group(1)
    assert "client.snapshot.board.card(forSession: row.agent.sessionId)" in body
    assert "onShowCard: bound == nil ? nil" in body
    assert "showCard(session: row.agent.sessionId)" in body
    assert "row.agent.isJumpable" in body
    assert "actions.jump(row.agent, client: client)" in body
