"""The phone reads the reply route; it never works one out.

`ios/` has no test target by explicit decision, so this is a Python lint over
the Swift source — `test_phone_needs_you.py`'s pattern. It pins that
`Models.swift` decodes `reply_via` tolerantly (an older Mac sends no key),
that `AnswerBox.swift`'s reply gate is still the Mac's `channel` and nothing
derived from `reply_via`, and that the placeholder names the typed route.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
ANSWER = PHONE / "AnswerBox.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "Models.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


@pytest.mark.parametrize("path", [MODELS, ANSWER, PANEL_MODELS])
def test_the_pinned_files_exist(path):
    _read(path)


def test_models_decode_reply_via_with_an_empty_default():
    src = _read(MODELS)
    assert src.count('case replyVia = "reply_via"') == 1
    assert re.search(r'replyVia = c\.value\(\.replyVia, ""\)', src)
    assert re.search(r'var replyVia = ""', src)


def test_the_panel_decodes_the_same_key_the_same_way():
    src = _read(PANEL_MODELS)
    assert src.count('case replyVia = "reply_via"') == 1
    assert re.search(r'replyVia = c\.value\(\.replyVia, ""\)', src)


def test_can_reply_is_still_stopped_and_channel_with_no_second_derivation():
    src = _read(ANSWER)
    gate = re.search(r"private var canReply: Bool \{ (.+?) \}", src)
    assert gate, "canReply gate missing"
    assert gate.group(1).startswith("stopped && agent.channel")
    assert "replyVia" not in gate.group(1)
    # The route word is drawn, never decided from.
    assert "agent.replyVia" in src
    for line in src.splitlines():
        if "replyVia" in line:
            assert "canReply" not in line and "canAnswer" not in line, line


def test_the_placeholder_names_the_typed_route():
    src = _read(ANSWER)
    assert 'agent.replyVia == "typed"' in src
    assert "typed into its terminal" in src
    assert "message to this agent" in src
