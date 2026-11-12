"""Both clients draw the daemon's `message_queue` and write none of it.

The daemon composes every word (`steering.compose_detail`); neither Swift
view holds a text literal, a clock, or a verb. No byte-equal twin is pinned
because there is no shared rule to keep equal. These are source pins in the
shape of `test_process_row_chips.py`."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"


def _read(path: Path) -> str:
    return path.read_text()


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


def _band() -> str:
    return _read(PANEL / "MessageQueueBand.swift")


def _phone_block() -> str:
    return _block(_read(PHONE / "AgentDetailView.swift"),
                  "private var messageQueueBlock")


def test_both_agents_decode_message_queue_tolerantly():
    for path in (PANEL / "Models.swift", PHONE / "Models.swift"):
        text = _read(path)
        assert text.count('case messageQueue = "message_queue"') == 1, path
        assert text.count("messageQueue = c.maybe(.messageQueue)") == 1, path
        model = _block(text, "struct MessageQueue")
        assert "try container" not in model
        assert "decode(" not in model.replace("decodeIfPresent", "")


def test_the_panel_band_and_the_phone_block_read_every_composed_field():
    for body in (_band(), _phone_block()):
        for name in ("headline", "label", "status", "excerpt", "why",
                     "moreLine"):
            assert name in body, name


def test_neither_view_holds_a_text_literal_a_clock_or_a_verb():
    for body in (_band(), _phone_block()):
        assert 'Text("' not in body
        for banned in ("createdAt", "Date(", "TimelineView", "Button",
                       "Panel.send", "post(", "onTapGesture", "swipeActions"):
            assert banned not in body, banned


def test_the_band_sits_under_the_header_before_the_terminal_branch():
    body = _block(_read(PANEL / "AgentDetailPane.swift"),
                  "var body: some View")
    assert body.index("MessageQueueBand(") < body.index(
        "DetailTab.terminalAttached")
    assert body.index("DetailTabBar(") < body.index("MessageQueueBand(")
    assert "agent.messageQueue" in body


def test_the_phone_block_sits_between_the_journey_and_the_verbs():
    main = _block(_read(PHONE / "AgentDetailView.swift"),
                  "private var mainScreen")
    assert (main.index("journey") < main.index("messageQueueBlock")
            < main.index("if hasVerbs"))


def test_the_phone_block_never_clips_prose():
    block = _phone_block()
    assert ".lineLimit(" not in block
    wrapping = re.findall(r"Text\(", block)
    assert wrapping and block.count(
        ".fixedSize(horizontal: false, vertical: true)") >= len(wrapping)


def test_the_clients_hold_no_steering_vocabulary():
    for root in (PANEL.parent, PHONE):
        for path in root.rglob("*.swift"):
            text = _read(path)
            for word in ("steer_queue", "STEER_", "steering"):
                assert word not in text, (path, word)
